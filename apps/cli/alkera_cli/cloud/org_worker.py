"""One org's worker: the cloud mirror service for one org, and nothing else.

Started by the box supervisor inside the org's namespaces
(``alkera_cli/supervisor/launch.py``), it reads its :class:`Hello` from the
socketpair the supervisor made, prepares its namespaces
(:mod:`alkera_cli.cloud.org_namespace`), and runs today's service in its org
mode: no claim and no heartbeat (the supervisor does both), only chats the
supervisor routed here, and of those only the ones whose own server row names
this org. What it holds it reports back as ids and counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import signal
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Final

from alkera_core.compute.box_isolation import ENV_ORG_ISOLATION, IsolationProfile

from alkera_cli.cloud.budget import TurnBudget
from alkera_cli.cloud.org_namespace import (
    ENV_LOCALDEV_FORWARD_PORTS,
    OrgNetwork,
    prepare,
    start_localdev_forwards,
)
from alkera_cli.cloud.publisher_identity import (
    DEFAULT_PROVIDER,
    ENV_MACHINE_PROVIDER,
    ENV_MACHINE_PROVIDER_POD_ID,
)
from alkera_cli.cloud.service import (
    ENV_API_URL,
    ENV_MACHINE_NAME,
    CloudMirrorService,
    MirrorSettings,
    drain_ceiling_from_env,
    memory_cap_from_env,
    mirror_limits_from_env,
)
from alkera_cli.cloud.sleep_policy import SleepSettings, parked_ask_hours_from_env
from alkera_cli.cloud.worker_credential import WorkerCredential
from alkera_cli.org_root import OrgRoot, ensure_org_subdirs
from alkera_cli.org_worker_channel import take_control_channel
from alkera_cli.org_worker_protocol import (
    Credential,
    Hello,
    ProtocolError,
    Ready,
    Refused,
    Route,
    Status,
    Stop,
    decode_to_worker,
    encode,
    read_line,
)

logger = logging.getLogger(__name__)

#: How often the worker tells the supervisor what it holds.
STATUS_INTERVAL_SECONDS: Final = 15.0


class OrgWorkerError(RuntimeError):
    """The worker cannot serve its org; it exits and the supervisor retries."""


def read_hello(fd: int) -> Hello:
    """The first frame, read before anything else runs: whom this worker
    serves. Anything but a :class:`Hello` ends it."""
    with os.fdopen(os.dup(fd), "rb", buffering=0) as stream:
        line = b""
        while not line.endswith(b"\n"):
            chunk = stream.read(1)
            if not chunk:
                raise OrgWorkerError("the supervisor closed the channel before saying hello")
            line += chunk
    frame = decode_to_worker(line)
    if not isinstance(frame, Hello):
        raise OrgWorkerError(f"the first frame was {frame.type}, not hello")
    return frame


def refuse_stored_login(home: Path) -> None:
    """A worker never runs on a person's session: a stored login in its home
    would be the identity every ambient reader acts as."""
    if (home / "auth.yml").exists():
        raise OrgWorkerError(
            f"a stored login exists at {home / 'auth.yml'}; an org worker refuses it"
        )


def rss_bytes() -> int:
    """This process's peak resident size (Linux reports KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def prepare_worker(root: OrgRoot) -> None:
    """The namespaces (only a worker started in its own has any to prepare:
    ``alkera_cli/supervisor/launch.py``), the org root's subdirectories, and
    the working directory the project lives in."""
    if os.environ.get(ENV_ORG_ISOLATION) != IsolationProfile.SINGLE_ORG:
        net = OrgNetwork(
            worker_ip=os.environ["ALKERA_ORG_WORKER_IP"], host_ip=os.environ["ALKERA_ORG_HOST_IP"]
        )
        prepare(net, pid=os.getpid())
    forwards = os.environ.get(ENV_LOCALDEV_FORWARD_PORTS, "").strip()
    if forwards:
        start_localdev_forwards(forwards)
    ensure_org_subdirs(root)
    refuse_stored_login(root.home)
    os.chdir(root.work)


def worker_settings(hello: Hello, *, project_dir: Path) -> MirrorSettings:
    """The service's settings for this org: the deployment's knobs from the
    environment the supervisor passed, the machine and the credential from
    the hello. Never a stored login, never the machine credential from the
    environment (the supervisor keeps it out of it)."""
    env = os.environ
    api_url = env.get(ENV_API_URL, "").strip()
    if not api_url:
        raise OrgWorkerError(f"an org worker needs {ENV_API_URL}")
    idle_minutes, max_mirrors = mirror_limits_from_env()
    return MirrorSettings(
        api_url=api_url,
        token=hello.credential,
        project_dir=project_dir,
        machine_name=env.get(ENV_MACHINE_NAME, "").strip() or f"org-{hello.slot}",
        budget=TurnBudget.from_env(),
        provider=env.get(ENV_MACHINE_PROVIDER, "").strip() or DEFAULT_PROVIDER,
        provider_pod_id=env.get(ENV_MACHINE_PROVIDER_POD_ID, "").strip(),
        machine_credential=hello.credential,
        mirror_idle_minutes=idle_minutes,
        max_mirrors=max_mirrors,
        memory_max_mirrors=memory_cap_from_env(),
        parked_ask_hours=parked_ask_hours_from_env(),
        drain_ceiling_seconds=drain_ceiling_from_env(),
        sleep=SleepSettings.from_env(),
        org_id=hello.org_id,
        machine_id=hello.machine_id,
    )


async def serve_control(
    service: CloudMirrorService,
    reader: asyncio.StreamReader,
    *,
    stop: asyncio.Event,
    on_credential: Callable[[Credential], None],
    report: Callable[[], None],
) -> None:
    """Apply what the supervisor says until it says stop or hangs up.

    Reading never waits on the work a frame starts: the pass a route asks
    for runs beside the reader, so a credential sent while a pass is busy
    (on a slow backend, or one refusing an expiring bearer) is taken at once,
    and the worker's status, which names the credential it holds, goes back
    on the spot.

    A stop says its kind (:class:`Stop`). A supervisor that hangs up without
    one (it was restarted, or it died) is a restart in place: the chats stay
    this box's, and the next supervisor starts the org's worker again, which
    takes them back with their leases."""
    passes = _RoutePasses(service)
    try:
        while (line := await read_line(reader)) is not None:
            frame = decode_to_worker(line)
            if isinstance(frame, Route):
                if service.org is not None:
                    service.org.route(frame.chat_ids)
                passes.request()
            elif isinstance(frame, Credential):
                on_credential(frame)
                report()
            elif isinstance(frame, Stop):
                service.begin_drain(restart=not frame.final)
                break
            else:
                raise ProtocolError("a second hello")
        else:
            service.begin_drain(restart=True)
    finally:
        passes.cancel()
        stop.set()


class _RoutePasses:
    """The sync passes routes ask for, one at a time: a route that lands while
    a pass runs is served by one more pass after it, however many land."""

    def __init__(self, service: CloudMirrorService) -> None:
        self._service = service
        self._task: asyncio.Task[None] | None = None
        self._again = False

    def request(self) -> None:
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        self._again = True
        while self._again:
            self._again = False
            try:
                await self._service.sync_once()
            except Exception:
                # The next request runs the pass again; one failure must not
                # end the loop that serves every later request.
                logger.exception("org worker: a requested chat sync failed")

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


def status_of(service: CloudMirrorService, credential: WorkerCredential) -> Status:
    """What the worker holds now, its credential included."""
    counts = service.activity_counts()
    return Status(
        chats_served=len(service.mirrors),
        chats_busy=counts.get("chats_busy", 0),
        chats_working=counts.get("chats_working"),
        rss_bytes=rss_bytes(),
        credential_seq=credential.seq,
        credential_refused=credential.refused,
    )


async def report_status(
    service: CloudMirrorService,
    credential: WorkerCredential,
    writer: asyncio.StreamWriter,
    *,
    interval: float,
) -> None:
    while True:
        writer.write(encode(status_of(service, credential)))
        await writer.drain()
        await asyncio.sleep(interval)


async def run_worker(
    settings: MirrorSettings,
    service: CloudMirrorService,
    control: socket.socket,
    *,
    credential: WorkerCredential,
) -> None:
    """Serve the org until the supervisor says stop, hangs up, or the service
    gives up."""
    reader, writer = await asyncio.open_connection(sock=control)

    said: set[tuple[str, str]] = set()

    def refused(chat_id: str, reason: str) -> None:
        # Once per chat and reason: every poll asks again.
        if (chat_id, reason) in said:
            return
        said.add((chat_id, reason))
        with contextlib.suppress(ProtocolError, ValueError):
            writer.write(encode(Refused(chat_id=chat_id, reason=reason)))

    if service.org is None:
        raise OrgWorkerError("an org worker's service has no org")
    service.org.on_refused = refused
    stop = asyncio.Event()

    def stopped_by_signal() -> None:
        # A stop from the service manager (the box shutting down, or its
        # backstop for a worker the supervisor could not stop) is final: every
        # chat is handed back within the drain ceiling, never cut.
        service.begin_drain(restart=False)
        stop.set()

    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(number, stopped_by_signal)

    def report() -> None:
        with contextlib.suppress(ProtocolError, ValueError):
            writer.write(encode(status_of(service, credential)))

    def take(frame: Credential) -> None:
        credential.replace(frame.credential, seq=frame.seq)

    credential.bind(report)

    control_task = asyncio.create_task(
        serve_control(service, reader, stop=stop, on_credential=take, report=report)
    )
    await service.start()
    writer.write(encode(Ready()))
    status_task = asyncio.create_task(
        report_status(service, credential, writer, interval=STATUS_INTERVAL_SECONDS)
    )
    try:
        await asyncio.wait(
            [asyncio.create_task(stop.wait()), asyncio.create_task(service.gave_up.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not service.gave_up.is_set():
            await service.drain()
    finally:
        status_task.cancel()
        control_task.cancel()
        await service.stop()
        writer.close()


__all__ = [
    "STATUS_INTERVAL_SECONDS",
    "OrgWorkerError",
    "prepare_worker",
    "read_hello",
    "refuse_stored_login",
    "report_status",
    "rss_bytes",
    "run_worker",
    "serve_control",
    "take_control_channel",
    "worker_settings",
]
