"""A worker's life under the supervisor: a supervisor restarted after a crash
claims and beats before it waits out the workers it found, a worker that
breaks the protocol or fails to start is ended (the unit, not only its
client), every systemctl call is bounded, the orgs to resume survive a crash,
and the events ops alarm on are emitted by name."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.org_root import OrgRoot
from alkera_cli.org_worker_protocol import Ready, Refused, encode
from alkera_cli.supervisor import crash_loop, org_events, org_units
from alkera_cli.supervisor import service as service_module
from alkera_cli.supervisor.org_resume import restart_window
from alkera_cli.supervisor.service import FileRoutingFeed, RouteEntry, Supervisor, Worker
from alkera_cli.supervisor.slots import Slot, SlotTable
from alkera_core.compute import box_logs, host_forward
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport

#: A box whose probe proved every mechanism but systemd: namespaced workers,
#: spawned directly.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})
#: The same on a systemd host: each worker a hardened unit.
ON_SYSTEMD = IsolationReport(frozenset(IsolationMechanism))

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


@contextlib.contextmanager
def capture_logs() -> Iterator[list[dict[str, Any]]]:
    """The supervisor's events, as the fields each line carries."""
    seen: list[dict[str, Any]] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            fields = getattr(record, "event_fields", {})
            seen.append({"event": record.msg, "log_level": record.levelname.lower(), **fields})

    log = logging.getLogger(org_events.LOGGER)
    keep, level = Keep(), log.level
    log.addHandler(keep)
    log.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        log.removeHandler(keep)
        log.setLevel(level)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)
        await asyncio.sleep(0)


class Backend:
    """The machine calls, recorded against the supervisor's clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[float, str, dict[str, object]]] = []

    async def claim(self, body: Mapping[str, object]) -> str:
        self.calls.append((self.clock.now, "claim", dict(body)))
        return "m-1"

    async def heartbeat(self, machine_id: str, body: Mapping[str, object]) -> None:
        self.calls.append((self.clock.now, "beat", dict(body)))

    async def worker_credential(self, org_id: str) -> tuple[str, float]:
        return f"alkm_org.{org_id}", 900.0


def _supervisor(
    tmp_path: Path, *, systemd: bool = True, env: dict[str, str] | None = None
) -> tuple[Supervisor, Clock, Backend, list[tuple[str, ...]]]:
    clock = Clock()
    backend = Backend(clock)
    sup = Supervisor(
        api=backend,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=SlotTable(tmp_path / "slots.json"),
        orgs_root=tmp_path / "orgs",
        env={"ALKERA_ORG_WORKER_BUDGET": "64", **(env or {})},
        isolation=ON_SYSTEMD if systemd else NAMESPACED,
        resume_path=tmp_path / "resume.json",
    )
    ran: list[tuple[str, ...]] = []
    sup._clock = lambda: clock.now
    sup._sleep = clock.sleep
    sup._runner = lambda argv: ran.append(tuple(argv)) or 0
    sup._unit_state = lambda unit: "inactive"
    return sup, clock, backend, ran


# -- a supervisor restarted after a crash ---------------------------------------


async def test_a_restarted_supervisor_claims_and_beats_before_it_waits_out_earlier_workers(
    tmp_path: Path,
) -> None:
    """A supervisor that sat in its wait for the workers an earlier one left
    sent no heartbeat for that whole time: the backend read the box as
    unreachable and moved every chat of every org off it. It claims first, and
    beats as restarting (the box keeps its chats) while it waits."""
    sup, clock, backend, ran = _supervisor(tmp_path)
    sup._slots.assign(ORG_A)
    sup._active_slots = lambda: {0}  # the earlier worker never ends on its own
    sup.stop()

    await sup.run()

    kill = ("systemctl", "kill", "--signal=KILL", "alkera-orgs.slice")
    assert kill in ran
    window = restart_window(sup._env)
    assert backend.calls[0][:2] == (0.0, "claim")
    beats = [(at, body) for at, kind, body in backend.calls if kind == "beat"]
    during = [(at, body) for at, body in beats if at < window]
    assert during and during[0][0] < 1.0
    assert all(body["restarting"] is True and body["draining"] is False for _, body in during)
    gaps = [b - a for (a, _), (b, _) in itertools.pairwise(during)]
    assert max(gaps) <= sup._beat + 1.0
    assert window <= clock.now < window + 2.0


async def test_the_orgs_whose_units_were_still_up_are_resumed(tmp_path: Path) -> None:
    """A supervisor that crashed named no org to resume, and the routing reads
    every chat of a restarting box as asleep: the orgs whose worker units the
    new supervisor finds running are the ones whose chats' leases it kept."""
    sup, _clock, _backend, _ran = _supervisor(tmp_path, env={"ALKERA_DAEMON_SUPERVISED": "1"})
    sup._slots.assign(ORG_A)
    held = sup._slots.assign(ORG_B)
    answers = iter([{held.index}])
    sup._active_slots = lambda: next(answers, set())
    sup.stop()

    await sup.run()

    assert json.loads((tmp_path / "resume.json").read_text()) == {"orgs": [ORG_B]}


async def test_the_orgs_to_resume_survive_a_crash_before_their_worker_is_ready(
    tmp_path: Path,
) -> None:
    """The list was deleted the moment it was read, so a supervisor that
    crashed before the workers came up left the next one nothing to resume."""
    (tmp_path / "resume.json").write_text(json.dumps({"orgs": [ORG_A]}))
    first, *_ = _supervisor(tmp_path, systemd=False)
    first._resume = first._take_resume()
    # ... and it dies here, before ORG_A's worker says it is ready.
    second, *_ = _supervisor(tmp_path, systemd=False)
    assert second._take_resume() == {ORG_A}


async def test_an_org_that_never_comes_up_is_wanted_only_for_the_hold(tmp_path: Path) -> None:
    from alkera_cli.supervisor.org_resume import RESUME_HOLD_SECONDS

    (tmp_path / "resume.json").write_text(json.dumps({"orgs": [ORG_A]}))
    sup, _clock, _backend, _ran = _supervisor(tmp_path, systemd=False)
    started: list[str] = []

    async def start(org_id: str) -> Worker:
        started.append(org_id)
        raise RuntimeError("no namespace")

    sup._start = start
    sup._resume = sup._take_resume()
    sup._resume_until = RESUME_HOLD_SECONDS
    await sup.reconcile([], now=RESUME_HOLD_SECONDS - 1)
    assert started == [ORG_A]
    await sup.reconcile([], now=RESUME_HOLD_SECONDS + 500)
    assert started == [ORG_A]
    assert not (tmp_path / "resume.json").exists()


# -- a worker's start and its end -------------------------------------------------

#: A worker that reads its hello, says what ``frames`` holds, then waits.
_WORKER = """
import os, sys, time
data = b""
while not data.endswith(b"\\n"):
    data += os.read(0, 65536)
os.write(0, sys.argv[1].encode())
time.sleep(60)
"""


def _rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, says: bytes, link_fails: bool = False
) -> tuple[Supervisor, list[tuple[str, ...]], list[asyncio.subprocess.Process]]:
    """A supervisor on a systemd host whose worker is a real process on the
    real socketpair; only the unit plumbing (systemctl, the namespace link)
    is stood in for."""
    sup, _clock, _backend, ran = _supervisor(tmp_path)
    sup._machine_id = "m-1"
    procs: list[asyncio.subprocess.Process] = []

    def root(orgs_root: Path, slot: Slot) -> OrgRoot:
        (orgs_root / str(slot.index)).mkdir(parents=True, exist_ok=True)
        return OrgRoot(orgs_root / str(slot.index))

    async def spawn(plan: Any, launch: Any, channel: Any) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _WORKER, says.decode(), stdin=channel.fileno()
        )
        procs.append(proc)
        return proc

    async def pid(plan: Any, process: asyncio.subprocess.Process) -> int:
        return process.pid

    async def link(launch: Any, pid: int) -> None:
        if link_fails:
            raise RuntimeError("could not link")

    monkeypatch.setattr(service_module, "ensure_org_root", root)
    monkeypatch.setattr(sup, "_spawn", spawn)
    monkeypatch.setattr(sup, "_worker_pid", pid)
    monkeypatch.setattr(sup, "_link", link)
    return sup, ran, procs


async def _ended(proc: asyncio.subprocess.Process) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        return False
    return True


async def test_a_worker_that_breaks_the_protocol_has_its_unit_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On systemd the spawned process is the systemd-run client; killing it
    alone left the unit serving the org."""
    sup, ran, procs = _rig(tmp_path, monkeypatch, says=b"not a frame\n")
    with capture_logs() as logs:
        worker = await sup._start(ORG_A)
        assert await _ended(procs[0])
        await asyncio.gather(*sup._listeners.values(), return_exceptions=True)

    assert ("systemctl", "kill", "--signal=KILL", f"alkera-org-{worker.slot.index}.service") in ran
    assert [e["event"] for e in logs if e["event"] == box_logs.WORKER_PROTOCOL_BROKEN]


async def test_a_worker_that_cannot_be_linked_is_ended_and_leaves_no_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, ran, procs = _rig(tmp_path, monkeypatch, says=encode(Ready()), link_fails=True)

    with pytest.raises(RuntimeError, match="could not link"):
        await sup._start(ORG_A)

    assert sup._listeners == {}
    assert ("systemctl", "kill", "--signal=KILL", "alkera-org-0.service") in ran
    assert await _ended(procs[0])


async def test_a_worker_that_says_it_is_ready_leaves_the_resume_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "resume.json").write_text(json.dumps({"orgs": [ORG_A, ORG_B]}))
    sup, _ran, procs = _rig(tmp_path, monkeypatch, says=encode(Ready()))
    sup._resume = sup._take_resume()

    with capture_logs() as logs:
        await sup._start(ORG_A)
        for _ in range(200):
            if ORG_A not in sup._resume:
                break
            await asyncio.sleep(0.05)

    assert json.loads((tmp_path / "resume.json").read_text()) == {"orgs": [ORG_B]}
    assert any(e["event"] == box_logs.WORKER_READY and e["org_id"] == ORG_A for e in logs)
    procs[0].kill()
    await procs[0].wait()


# -- events ops alarm on -----------------------------------------------------------


async def test_refusals_are_counted_events_with_a_fixed_reason_code(tmp_path: Path) -> None:
    sup, *_ = _supervisor(tmp_path)
    slot = sup._slots.assign(ORG_A)
    reader = asyncio.StreamReader()
    for chat, reason in (
        ("c1", "the chat's row names another org"),
        ("c2", "the supervisor has not routed the chat here"),
        ("c3", "anything a worker made up, prompt text included"),
    ):
        reader.feed_data(encode(Refused(chat_id=chat, reason=reason)))
    reader.feed_eof()

    async def send(frame: Any) -> None:
        return None

    worker = Worker(slot=slot, alive=lambda: True, send=send)
    with capture_logs() as logs:
        await sup._listen(worker, reader)

    refused = [e for e in logs if e["event"] == box_logs.ORG_ADMISSION_REFUSED]
    assert [(e["chat_id"], e["reason"], e["count"]) for e in refused] == [
        ("c1", "another_org", 1),
        ("c2", "not_routed", 2),
        ("c3", "other", 3),
    ]
    assert [e["log_level"] for e in refused] == ["error", "warning", "warning"]
    assert all("prompt" not in str(e) for e in refused)


def test_the_reason_codes_match_what_a_worker_refuses_with() -> None:
    """The supervisor cannot import the worker's admission (it touches tenant
    content); the two are held together here."""
    from alkera_cli.cloud.org_admission import OrgAdmission

    admission = OrgAdmission(ORG_A)
    admission.route(["c1"])
    foreign = admission.refusal({"id": "c1", "org_id": ORG_B})
    unrouted = admission.refusal({"id": "c9", "org_id": ORG_A})
    assert foreign is not None and unrouted is not None
    assert org_events.refusal_code(foreign) == "another_org"
    assert org_events.refusal_code(unrouted) == "not_routed"


class _Fleet:
    def __init__(self, table: SlotTable) -> None:
        self.table = table
        self.dead: set[str] = set()
        self.starts = 0

    async def start(self, org_id: str) -> Worker:
        self.starts += 1
        slot = self.table.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: org_id not in self.dead, send=send)


async def _exits(sup: Supervisor, fleet: _Fleet, *, times: int, every: float) -> float:
    routes = [RouteEntry("a1", ORG_A)]
    now = 0.0
    await sup.reconcile(routes, now=now)
    for _ in range(times):
        now += every
        fleet.dead.add(ORG_A)
        await sup.reconcile(routes, now=now)
        fleet.dead.discard(ORG_A)
        before = fleet.starts
        while fleet.starts == before:
            now += 1.0
            await sup.reconcile(routes, now=now)
    return now


@pytest.mark.parametrize(
    ("times", "every", "loops"),
    [
        pytest.param(crash_loop.CRASH_LOOP_EXITS + 1, 1.0, True, id="one-past-the-limit-fast"),
        pytest.param(crash_loop.CRASH_LOOP_EXITS, 1.0, False, id="at-the-limit"),
        pytest.param(crash_loop.CRASH_LOOP_EXITS + 3, 240.0, False, id="spread-out"),
    ],
)
async def test_a_worker_that_keeps_exiting_is_reported_as_a_crash_loop(
    tmp_path: Path, times: int, every: float, loops: bool
) -> None:
    sup, *_ = _supervisor(tmp_path, systemd=False)
    fleet = _Fleet(sup._slots)
    sup._start = fleet.start
    with capture_logs() as logs:
        await _exits(sup, fleet, times=times, every=every)
    looped = [e for e in logs if e["event"] == box_logs.WORKER_CRASH_LOOP]
    exited = [e for e in logs if e["event"] == box_logs.WORKER_EXITED]
    assert len(exited) == times
    if loops:
        assert len(looped) == 1
        event = looped[0]
        assert (event["slot"], event["restarts"]) == (0, times)
        assert event["backoff"] > 0 and event["log_level"] == "error"
    else:
        assert looped == []


def test_every_event_is_named_and_an_unknown_one_is_refused() -> None:
    assert all(name.startswith("supervisor.") for name in org_events.EVENTS)
    assert len(set(org_events.EVENTS)) == len(org_events.EVENTS)
    with pytest.raises(ValueError, match="not a supervisor event"):
        org_events.emit("supervisor.something_made_up")


# -- bounded systemctl -------------------------------------------------------------


def test_a_systemctl_call_that_times_out_is_a_failure_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=30)

    monkeypatch.setattr(org_units, "run_child", hang)
    with capture_logs() as logs:
        assert org_units.run(("systemctl", "stop", "alkera-org-3.service")) == org_units.TIMED_OUT
        assert org_units.active_org_slots() == set()
        assert org_units.unit_state("alkera-org-3.service") == "unknown"
    assert [e["event"] for e in logs] == [box_logs.UNIT_COMMAND_TIMED_OUT] * 3


async def test_a_supervisor_outlasting_a_hung_systemd_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=30)

    monkeypatch.setattr(org_units, "run_child", hang)
    sup, *_ = _supervisor(tmp_path)
    sup._runner = org_units.run
    sup._active_slots = lambda: {0}
    await sup._outlast_earlier_workers()  # returns: the kill timed out, nothing raised


@pytest.mark.parametrize(
    ("states", "killed", "gone"),
    [
        pytest.param(["active", "deactivating", "inactive"], False, True, id="stops-in-time"),
        pytest.param(["deactivating"] * 35 + ["inactive"], True, True, id="draining-is-killed"),
        pytest.param(["deactivating"] * 1000, True, False, id="never-goes"),
        pytest.param(["failed"], False, True, id="failed-is-gone"),
    ],
)
async def test_a_leftover_unit_is_stopped_without_blocking_then_killed(
    states: list[str], killed: bool, gone: bool
) -> None:
    """A blocking ``systemctl stop`` waits for the unit's worker to drain,
    which may take hours, and timed out at 30 s; a unit still deactivating
    holds its name, so the next worker could not take it."""
    clock = Clock()
    ran: list[tuple[str, ...]] = []
    answers = iter(states)

    async def runner(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return 0

    async def state(unit: str) -> str:
        return next(answers, states[-1])

    unit = "alkera-org-4.service"
    result = await org_units.clear_unit(unit, runner=runner, state=state, sleep=clock.sleep)

    assert result is gone
    assert ran[0] == ("systemctl", "stop", "--no-block", unit)
    assert (("systemctl", "kill", "--signal=KILL", unit) in ran) is killed
    assert ran[-1] == ("systemctl", "reset-failed", unit)
    bound = org_units.UNIT_STOP_GRACE_SECONDS + org_units.UNIT_KILL_GRACE_SECONDS
    assert clock.now <= bound


def test_the_active_units_name_their_slots() -> None:
    listing = (
        "alkera-org-3.service loaded active running alkera worker\n"
        "alkera-org-12.service loaded active running\n"
        "alkera-orgs.slice loaded active active\n"
        "\n"
        "alkera-org-x.service loaded active running\n"
    )
    assert org_units.parse_unit_slots(listing) == {3, 12}


def test_an_event_is_one_json_line_with_its_name_and_fields() -> None:
    record = logging.LogRecord(
        org_events.LOGGER, logging.ERROR, __file__, 1, box_logs.WORKER_CRASH_LOOP, None, None
    )
    record.event_fields = {"slot": 3, "restarts": 6}
    line = org_events.JsonLines().format(record)
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["event"] == "supervisor.worker.crash_loop"
    assert (parsed["level"], parsed["slot"], parsed["restarts"]) == ("error", 3, 6)
    assert parsed["timestamp"].endswith("+00:00")


async def test_a_heartbeat_puts_back_the_accepts_a_restarted_docker_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Docker that restarts rebuilds its chains; one whose DOCKER-USER comes
    back empty would cut every running worker off until the node restarted.
    The next heartbeat puts the accepts back, once."""
    sup, _clock, _backend, _ran = _supervisor(tmp_path)
    sup._machine_id = "m-1"
    sup._env["ALKERA_SANDBOX_MODE"] = "gvisor"
    docker_user: list[tuple[str, ...]] = []

    def iptables(argv: Sequence[str]) -> int:
        if argv[0] != "iptables" or argv[3] != "DOCKER-USER":
            return 1  # no ip6tables, ufw or firewalld on this host
        _binary, _wait, verb, _chain, *rule = argv
        if verb == "-C":
            return 0 if tuple(rule) in docker_user else 1
        if verb == "-I":
            docker_user.insert(int(rule[0]) - 1, tuple(rule[1:]))
        return 0

    real = host_forward.open_links

    def apply(env: Mapping[str, str], prefixes: Sequence[str]) -> tuple[str, ...]:
        return real(env, prefixes, host=host_forward.Host(run=iptables))

    monkeypatch.setattr(host_forward, "open_links", apply)
    await sup.beat()
    applied = list(docker_user)
    docker_user.clear()  # Docker restarted and made its chain anew

    await sup.beat()
    await sup.beat()

    assert applied and docker_user == applied
