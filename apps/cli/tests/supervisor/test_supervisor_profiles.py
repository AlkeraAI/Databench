"""The supervisor under each isolation profile: a box that cannot keep orgs
apart still serves its one org, refuses a second, says why a worker will not
start in the events it ships, and says on its heartbeat when it can serve
nobody. Every event about an org's worker carries that worker's org."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.org_root import OrgRoot
from alkera_cli.supervisor import org_events
from alkera_cli.supervisor import service as service_module
from alkera_cli.supervisor.crash_loop import CRASH_LOOP_FAILURES
from alkera_cli.supervisor.service import FileRoutingFeed, RouteEntry, Supervisor, Worker
from alkera_cli.supervisor.service import probe_isolation as probe
from alkera_cli.supervisor.slots import Slot, SlotTable
from alkera_core.compute import box_logs
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport
from alkera_core.compute.box_logs import sanitize
from alkera_core.compute.worker_faults import WorkerFault
from alkera_core.host_isolation import Host

needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="the worker's socketpair on standard input is POSIX"
)

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})
SINGLE_ORG = IsolationReport(frozenset())

#: The first word of every command a namespaced launch runs on the host.
NAMESPACE_COMMANDS = frozenset({"/bin/sh", "mkdir", "chown", "unshare", "ip", "systemd-run"})


@contextlib.contextmanager
def events() -> Iterator[list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append({"event": record.msg, **getattr(record, "event_fields", {})})

    log = logging.getLogger(org_events.LOGGER)
    keep, level = Keep(), log.level
    log.addHandler(keep)
    log.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        log.removeHandler(keep)
        log.setLevel(level)


class Backend:
    def __init__(self) -> None:
        self.beats: list[dict[str, object]] = []

    async def claim(self, body: Mapping[str, object]) -> str:
        return "m-1"

    async def heartbeat(self, machine_id: str, body: Mapping[str, object]) -> None:
        self.beats.append(dict(body))

    async def worker_credential(self, org_id: str) -> tuple[str, float]:
        return f"alkm_org.{org_id}", 900.0


def _runpod_report(tmp_path: Path) -> IsolationReport:
    """What the probe finds in a RunPod pod (``test_org_isolation.py`` pins
    each primitive)."""

    def run(argv: Sequence[str]) -> int:
        return 1 if any(w in " ".join(argv) for w in ("--mount", "--map-users", "--net")) else 0

    def write(path: Path, text: str) -> None:
        raise OSError(errno.EIO, "Input/output error")

    return probe(
        Host(
            run=run,
            write=write,
            which=lambda name: f"/usr/bin/{name}",
            systemd=lambda: False,
            root=True,
            cgroup_root=tmp_path / "cgroup",
        )
    )


def _supervisor(
    tmp_path: Path, isolation: IsolationReport, **kwargs: Any
) -> tuple[Supervisor, Backend, list[tuple[str, ...]]]:
    backend = Backend()
    sup = Supervisor(
        api=backend,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=SlotTable(tmp_path / "slots.json"),
        orgs_root=tmp_path / "orgs",
        env={"ALKERA_ORG_WORKER_BUDGET": "64", "PATH": "/usr/bin:/bin"},
        isolation=isolation,
        log_dir=tmp_path / "logs",
        **kwargs,
    )
    sup._machine_id = "m-1"
    ran: list[tuple[str, ...]] = []
    sup._runner = lambda argv: ran.append(tuple(argv)) or 0
    return sup, backend, ran


#: A worker that reads its hello, notes the profile it was told and its own
#: argv, says it is ready, then waits.
_WORKER = """
import os, sys, time
data = b""
while not data.endswith(b"\\n"):
    data += os.read(0, 65536)
with open(sys.argv[1], "w") as out:
    out.write(os.environ.get("ALKERA_ORG_ISOLATION", "") + "\\n" + " ".join(sys.argv[2:4]))
os.write(0, b'{"type": "ready"}\\n')
time.sleep(60)
"""


@needs_posix
async def test_a_runpod_box_starts_its_org_s_worker_with_no_cgroup_or_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box this was found on: every turn stayed pending because each
    worker start ran ``cgroup_steps`` and died on the pod's root cgroup."""
    report = _runpod_report(tmp_path)
    sup, _backend, ran = _supervisor(tmp_path, report)
    said = tmp_path / "worker-said.txt"

    def root(orgs_root: Path, slot: Slot) -> OrgRoot:
        (orgs_root / str(slot.index)).mkdir(parents=True, exist_ok=True)
        return OrgRoot(orgs_root / str(slot.index))

    monkeypatch.setattr(service_module, "ensure_org_root", root)
    monkeypatch.setattr(
        service_module, "_alkera_argv", lambda: [sys.executable, "-c", _WORKER, str(said)]
    )
    with events() as seen:
        worker = await sup._start(ORG_A)
        for _ in range(200):
            if any(e["event"] == box_logs.WORKER_READY for e in seen):
                break
            await asyncio.sleep(0.05)
    try:
        assert worker.alive()
        assert [e["org_id"] for e in seen if e["event"] == box_logs.WORKER_READY] == [ORG_A]
        assert not [argv for argv in ran if argv[0] in NAMESPACE_COMMANDS]
        # The worker is the worker, not a chain of shells and unshares, and it
        # was told it has no namespaces of its own to prepare.
        profile, argv = said.read_text().splitlines()
        assert profile == "single_org"
        assert argv == "cloud-mirror worker"
    finally:
        assert worker.kill is not None
        await worker.kill()


async def test_a_single_org_box_refuses_a_second_org_and_keeps_the_first(
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def start(org_id: str) -> Worker:
        if not sup._isolation.admits(org_id, (s.org_id for s in sup._slots.slots())):
            return await Supervisor._start_worker(sup, org_id)
        started.append(org_id)
        slot = sup._slots.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: True, send=send)

    sup, _backend, _ran = _supervisor(tmp_path, SINGLE_ORG, start_worker=start)
    with events() as seen:
        await sup.reconcile(
            [RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open")], now=0.0
        )
        await sup.reconcile(
            [
                RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open"),
                RouteEntry(chat_id="c-b", org_id=ORG_B, desired_state="open"),
            ],
            now=10.0,
        )
    assert started == [ORG_A]
    assert set(sup.workers) == {ORG_A}
    failed = [e for e in seen if e["event"] == box_logs.WORKER_START_FAILED]
    assert [(e["org_id"], e["reason"]) for e in failed] == [
        (ORG_B, WorkerFault.SECOND_ORG_REFUSED.value)
    ]
    # Nothing of the second org was made on the box.
    assert sup._slots.find(ORG_B) is None


async def test_a_namespaced_box_takes_a_second_org(tmp_path: Path) -> None:
    async def start(org_id: str) -> Worker:
        slot = sup._slots.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: True, send=send)

    sup, _backend, _ran = _supervisor(tmp_path, NAMESPACED, start_worker=start)
    await sup.reconcile(
        [
            RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open"),
            RouteEntry(chat_id="c-b", org_id=ORG_B, desired_state="open"),
        ],
        now=0.0,
    )
    assert set(sup.workers) == {ORG_A, ORG_B}


def test_the_heartbeat_states_the_profile_and_only_a_namespaced_box_claims_isolation(
    tmp_path: Path,
) -> None:
    single, *_ = _supervisor(tmp_path / "a", SINGLE_ORG)
    namespaced, *_ = _supervisor(tmp_path / "b", NAMESPACED)
    assert BoxCapability.ORG_ISOLATION not in single.heartbeat_body()["capabilities"]  # type: ignore[operator]
    assert single.heartbeat_body()["isolation"] == {"profile": "single_org", "mechanisms": []}
    assert BoxCapability.ORG_ISOLATION in namespaced.heartbeat_body()["capabilities"]  # type: ignore[operator]


# -- a worker that cannot start is the box's fault, said on the beat ------------


async def test_a_box_whose_only_org_keeps_failing_says_so_until_a_worker_serves(
    tmp_path: Path,
) -> None:
    fail = True

    async def start(org_id: str) -> Worker:
        if fail:
            raise RuntimeError("unshare: unshare failed: Operation not permitted")
        slot = sup._slots.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: True, send=send)

    sup, _backend, _ran = _supervisor(tmp_path, SINGLE_ORG, start_worker=start)
    routes = [RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open")]
    now = 0.0
    with events() as seen:
        for _ in range(CRASH_LOOP_FAILURES - 1):
            await sup.reconcile(routes, now=now)
            now += 1000.0
        assert sup.heartbeat_body()["fault"] is None  # not yet a crash loop
        await sup.reconcile(routes, now=now)
    assert sup.heartbeat_body()["fault"] == {
        "code": "other",
        "summary": "unshare: unshare failed: Operation not permitted",
    }
    # Every failure says why on the event that leaves the box.
    failed = [e for e in seen if e["event"] == box_logs.WORKER_START_FAILED]
    assert len(failed) == CRASH_LOOP_FAILURES
    shipped = sanitize(failed[-1]["event"], "warning", None, failed[-1])
    assert shipped is not None
    assert shipped["reason"] == "other" and shipped["org_id"] == ORG_A
    assert shipped["error"] == "unshare: unshare failed: Operation not permitted"

    fail = False
    await sup.reconcile(routes, now=now + 1000.0)
    assert ORG_A in sup.workers
    assert sup.heartbeat_body()["fault"] is None


async def test_a_box_with_nothing_wanted_of_it_has_no_fault(tmp_path: Path) -> None:
    async def start(org_id: str) -> Worker:
        raise RuntimeError("never")

    sup, _backend, _ran = _supervisor(tmp_path, SINGLE_ORG, start_worker=start)
    for n in range(CRASH_LOOP_FAILURES):
        await sup.reconcile(
            [RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open")], now=n * 1000.0
        )
    await sup.reconcile(
        [RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="asleep")], now=10_000.0
    )
    assert sup.heartbeat_body()["fault"] is None


async def test_one_org_failing_on_a_box_serving_another_is_not_the_box_s_fault(
    tmp_path: Path,
) -> None:
    async def start(org_id: str) -> Worker:
        if org_id == ORG_B:
            raise RuntimeError("no")
        slot = sup._slots.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: True, send=send)

    sup, _backend, _ran = _supervisor(tmp_path, NAMESPACED, start_worker=start)
    routes = [
        RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open"),
        RouteEntry(chat_id="c-b", org_id=ORG_B, desired_state="open"),
    ]
    for n in range(CRASH_LOOP_FAILURES):
        await sup.reconcile(routes, now=n * 1000.0)
    assert ORG_B in sup.failing
    assert sup.heartbeat_body()["fault"] is None


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        pytest.param(
            service_module.WorkerStartError(WorkerFault.CGROUP_REFUSED, "x"),
            "cgroup_refused",
            id="named",
        ),
        pytest.param(OSError(errno.ENOENT, "no alkera"), "spawn_failed", id="spawn"),
        pytest.param(service_module.SlotError("full"), "slots_exhausted", id="slots"),
        pytest.param(service_module.ApiError(404, "gone"), "credential_refused", id="credential"),
        pytest.param(ValueError("odd"), "other", id="anything-else"),
    ],
)
async def test_every_start_failure_ships_a_code(
    tmp_path: Path, raised: Exception, code: str
) -> None:
    async def start(org_id: str) -> Worker:
        raise raised

    sup, _backend, _ran = _supervisor(tmp_path, SINGLE_ORG, start_worker=start)
    with events() as seen:
        await sup.reconcile(
            [RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open")], now=0.0
        )
    (failed,) = [e for e in seen if e["event"] == box_logs.WORKER_START_FAILED]
    assert failed["reason"] == code
    assert failed["org_id"] == ORG_A


# -- each event carries the org of the worker it describes ----------------------


async def test_two_workers_events_each_carry_their_own_org(tmp_path: Path) -> None:
    """An exit of B's worker was logged under A: the event named no org, and
    the line took one from whatever was in scope where it was logged."""
    alive = {ORG_A: True, ORG_B: True}

    async def start(org_id: str) -> Worker:
        slot = sup._slots.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: alive[org_id], send=send, started_at=0.0)

    sup, _backend, _ran = _supervisor(
        tmp_path, NAMESPACED, start_worker=start, idle_grace_seconds=1.0
    )
    routes = [
        RouteEntry(chat_id="c-a", org_id=ORG_A, desired_state="open"),
        RouteEntry(chat_id="c-b", org_id=ORG_B, desired_state="open"),
    ]
    with events() as seen:
        await sup.reconcile(routes, now=0.0)
        alive[ORG_B] = False
        await sup.reconcile(routes[:1], now=5.0)
        # A's chats are gone: past the grace its worker is told to stop.
        await sup.reconcile([], now=10.0)
        await sup.reconcile([], now=20.0)
    slots = {org: sup._slots.find(org) for org in (ORG_A, ORG_B)}
    about_a_worker = [e for e in seen if e["event"] in box_logs.ORG_EVENTS]
    assert {e["event"] for e in about_a_worker} >= {
        box_logs.WORKER_EXITED,
        box_logs.WORKER_STOPPING,
    }
    for event in about_a_worker:
        held = slots[event["org_id"]]
        assert held is not None and event["slot"] == held.index, event
    exited = [e for e in seen if e["event"] == box_logs.WORKER_EXITED]
    assert [e["org_id"] for e in exited] == [ORG_B]


def test_an_event_about_an_org_that_names_none_is_refused() -> None:
    with pytest.raises(ValueError, match="must name it"):
        org_events.emit(box_logs.WORKER_EXITED, slot=2, backoff=2.0, ran=1.0)
    with pytest.raises(TypeError):
        # The slot's own org can never be overridden by the caller.
        org_events.emit_for(Slot(index=2, org_id=ORG_B), box_logs.WORKER_KILLED, org_id=ORG_A)
