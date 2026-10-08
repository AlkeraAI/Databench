"""A stopping supervisor drains its workers the way the single daemon drained:
a final stop hands every chat back within the drain ceiling, a restart keeps
them, the box beats through either, and only a worker that outlives its
window is killed."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.org_worker_protocol import Credential, Stop, decode_to_worker, encode
from alkera_cli.supervisor.launch import WorkerLaunch, unit_argv
from alkera_cli.supervisor.service import (
    FileRoutingFeed,
    RouteEntry,
    Supervisor,
    Worker,
    drain_window,
    stop_is_final,
)
from alkera_cli.supervisor.slots import SlotTable
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport
from alkera_core.compute.liveness import (
    STOP_EXIT_SECONDS,
    unit_stop_timeout_seconds,
)

#: A box whose probe proved every mechanism but systemd: namespaced workers,
#: spawned directly.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})


ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


class Box:
    """Fake workers that end when told to (or never), a fake API that records
    the beats, and a clock that only the supervisor's own sleeps move."""

    def __init__(self, table: SlotTable) -> None:
        self.table = table
        self.now = 0.0
        self.told: dict[str, list[object]] = {}
        #: Per org: how long after its stop it ends; ``None`` never.
        self.ends_after: dict[str, float | None] = {}
        self.stopped_at: dict[str, float] = {}
        self.killed: dict[str, float] = {}
        self.beats: list[tuple[float, dict[str, object]]] = []
        self.minted: list[tuple[float, str]] = []

    async def start(self, org_id: str) -> Worker:
        slot = self.table.assign(org_id)
        told = self.told.setdefault(org_id, [])

        async def send(frame: Any) -> None:
            told.append(frame)
            if isinstance(frame, Stop):
                self.stopped_at.setdefault(org_id, self.now)

        def alive() -> bool:
            if org_id in self.killed:
                return False
            after = self.ends_after.get(org_id)
            stopped = self.stopped_at.get(org_id)
            return after is None or stopped is None or self.now < stopped + after

        async def kill() -> None:
            self.killed[org_id] = self.now

        return Worker(slot=slot, alive=alive, send=send, started_at=self.now, kill=kill)

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)

    async def heartbeat(self, machine_id: str, body: Mapping[str, object]) -> None:
        self.beats.append((self.now, dict(body)))

    async def worker_credential(self, org_id: str) -> tuple[str, float]:
        self.minted.append((self.now, org_id))
        return f"alkm_org.{org_id}", 900.0


async def _draining(tmp_path: Path, env: dict[str, str], *orgs: str) -> tuple[Supervisor, Box]:
    table = SlotTable(tmp_path / "slots.json")
    return await _supervisor_on(Box(table), table, tmp_path, env, *orgs)


async def _supervisor_on(
    box: Box, table: SlotTable, tmp_path: Path, env: dict[str, str], *orgs: str
) -> tuple[Supervisor, Box]:
    sup = Supervisor(
        api=box,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=table,
        env={"ALKERA_ORG_WORKER_BUDGET": "64", **env},
        start_worker=box.start,
        isolation=NAMESPACED,
        tick_seconds=3.0,
        beat_seconds=20.0,
        resume_path=tmp_path / "resume.json",
    )
    sup._clock = lambda: box.now
    sup._sleep = box.sleep
    sup._machine_id = "m-1"
    await sup.reconcile([RouteEntry(f"chat-{org}", org) for org in orgs], now=0)
    return sup, box


@pytest.mark.parametrize(
    ("env", "final_file", "final"),
    [
        pytest.param({}, False, True, id="unsupervised-every-stop-is-final"),
        pytest.param({"ALKERA_DAEMON_SUPERVISED": "1"}, False, False, id="supervised-restart"),
        pytest.param({"ALKERA_DAEMON_SUPERVISED": "1"}, True, True, id="supervised-final-file"),
        pytest.param({"ALKERA_DAEMON_SUPERVISED": "0"}, True, True, id="not-supervised"),
    ],
)
def test_the_stop_kind_follows_the_single_daemons_rule(
    tmp_path: Path, env: dict[str, str], final_file: bool, final: bool
) -> None:
    marker = tmp_path / "daemon.final-stop"
    if final_file:
        marker.write_text("")
    full = {**env, "ALKERA_DAEMON_FINAL_STOP_FILE": str(marker)}
    assert stop_is_final(full) is final


def test_a_supervised_stop_naming_no_final_file_is_a_restart() -> None:
    assert stop_is_final({"ALKERA_DAEMON_SUPERVISED": "1"}) is False


@pytest.mark.parametrize(
    ("env", "window"),
    [
        pytest.param({}, 6 * 3600 + STOP_EXIT_SECONDS, id="default-ceiling"),
        pytest.param(
            {"ALKERA_CLOUD_DRAIN_CEILING_SECONDS": "600"}, 600 + STOP_EXIT_SECONDS, id="named"
        ),
        pytest.param(
            {"ALKERA_CLOUD_DRAIN_CEILING_SECONDS": "0"}, STOP_EXIT_SECONDS, id="zero-kept"
        ),
    ],
)
def test_the_drain_window(env: dict[str, str], window: float) -> None:
    """A final stop waits for its workers to hand every chat back, up to the
    drain ceiling and the margin a worker takes to end itself."""
    assert drain_window(env) == window


async def test_a_final_stop_tells_every_worker_and_waits_for_each_to_end(tmp_path: Path) -> None:
    sup, box = await _draining(tmp_path, {}, ORG_A, ORG_B)
    box.ends_after = {ORG_A: 40.0, ORG_B: 400.0}

    await sup.drain(final=True)

    assert box.told[ORG_A][-1] == Stop(final=True)
    assert box.told[ORG_B][-1] == Stop(final=True)
    # The supervisor returned once the slower one ended, and cut neither.
    assert box.killed == {}
    assert 400.0 <= box.now < 404.0


async def test_the_box_beats_through_a_drain_saying_it_drains(tmp_path: Path) -> None:
    sup, box = await _draining(tmp_path, {}, ORG_A)
    box.ends_after = {ORG_A: 100.0}

    await sup.drain(final=True)

    times = [at for at, _ in box.beats]
    assert len(times) >= 5 and max(b - a for a, b in itertools.pairwise(times)) <= 23
    assert all(body["draining"] is True and body["restarting"] is None for _, body in box.beats)


async def test_a_restart_keeps_the_chats_and_says_so_on_the_beat(tmp_path: Path) -> None:
    sup, box = await _draining(tmp_path, {"ALKERA_DAEMON_SUPERVISED": "1"}, ORG_A)
    box.ends_after = {ORG_A: 10.0}

    await sup.drain(final=stop_is_final(sup._env))

    assert box.told[ORG_A][-1] == Stop(final=False)
    assert box.beats and all(
        body["draining"] is False and body["restarting"] is True for _, body in box.beats
    )


async def test_a_restart_kills_a_worker_at_the_restart_window_not_the_drain_ceiling(
    tmp_path: Path,
) -> None:
    """A worker restarting in place waits minutes for a background job, so
    the supervisor's restart has no business waiting the six-hour ceiling for
    one that is stuck."""
    sup, box = await _draining(tmp_path, {"ALKERA_DAEMON_SUPERVISED": "1"}, ORG_A)
    box.ends_after = {ORG_A: None}

    await sup.drain(final=False)

    window = 600 + 30 + STOP_EXIT_SECONDS
    assert window <= box.killed[ORG_A] < window + 3.1


async def test_a_worker_that_outlives_its_window_is_killed_then_and_not_before(
    tmp_path: Path,
) -> None:
    env = {"ALKERA_CLOUD_DRAIN_CEILING_SECONDS": "60"}
    sup, box = await _draining(tmp_path, env, ORG_A, ORG_B)
    box.ends_after = {ORG_A: None, ORG_B: 30.0}

    await sup.drain(final=True)

    window = 60 + STOP_EXIT_SECONDS
    assert set(box.killed) == {ORG_A}
    assert window <= box.killed[ORG_A] < window + 3.1


async def test_a_long_drain_keeps_the_workers_credentials_fresh(tmp_path: Path) -> None:
    """A drain pushes folders and releases leases on the worker's credential;
    one that ran out mid-drain would strand every chat it had not yet handed
    back."""
    sup, box = await _draining(tmp_path, {}, ORG_A)
    box.ends_after = {ORG_A: 2000.0}
    sup.workers[ORG_A].credential.refresh_at = 500.0

    await sup.drain(final=True)

    refreshed = [f for f in box.told[ORG_A] if isinstance(f, Credential)]
    assert len(refreshed) >= 3
    assert all(at >= 500.0 for at, _ in box.minted)


def test_the_stop_frame_carries_its_kind_and_defaults_to_final() -> None:
    assert decode_to_worker(encode(Stop(final=False))) == Stop(final=False)
    assert decode_to_worker(b'{"type": "stop"}\n') == Stop(final=True)


def test_systemd_stopping_a_worker_itself_signals_it_alone_and_waits_past_its_bound(
    tmp_path: Path,
) -> None:
    """The box shutting down stops the org's unit directly. Its agent servers
    must outlive the worker's drain (only the worker is signalled), the
    network must still be up while it hands chats back, and the kill must
    wait past the worker's own bound."""
    table = SlotTable(tmp_path / "slots.json")
    launch = WorkerLaunch(
        slot=table.assign(ORG_A),
        org_root=tmp_path / "orgs" / "0",
        argv=("alkera",),
        stop_timeout_seconds=unit_stop_timeout_seconds(3600),
    )
    argv = unit_argv(launch)
    assert "--property=KillMode=mixed" in argv
    assert "--property=After=network-online.target" in argv
    assert f"--property=TimeoutStopSec={unit_stop_timeout_seconds(3600)}" in argv
    assert unit_stop_timeout_seconds(3600) > 3600 + STOP_EXIT_SECONDS


@pytest.mark.parametrize(
    ("env", "busy_polls", "killed_at"),
    [
        pytest.param({}, 3, None, id="earlier-workers-end-on-their-own"),
        pytest.param(
            {"ALKERA_CLOUD_DRAIN_CEILING_SECONDS": "60"},
            10_000,
            60 + 30 + STOP_EXIT_SECONDS,
            id="never-short-drain-ceiling",
        ),
        pytest.param({}, 10_000, 600 + 30 + STOP_EXIT_SECONDS, id="never-default-ceiling"),
    ],
)
async def test_a_restarted_supervisor_lets_earlier_workers_finish_then_kills_what_is_left(
    tmp_path: Path, env: dict[str, str], busy_polls: int, killed_at: float | None
) -> None:
    """Workers an earlier supervisor left drain as a restart once its channel
    closes; killing their slice at once would cut the turns they were
    settling. The new supervisor waits for them within a restart's window
    (minutes, not the six-hour drain ceiling), then kills what is left:
    stopping it would make each hand its chats back over the whole ceiling."""
    sup, box = await _draining(tmp_path, env)
    polls = iter(range(busy_polls))
    ran: list[tuple[float, tuple[str, ...]]] = []
    sup._active_slots = lambda: {0} if next(polls, None) is not None else set()
    sup._runner = lambda argv: ran.append((box.now, tuple(argv))) or 0

    await sup._outlast_earlier_workers()

    if killed_at is None:
        assert ran == []
        assert box.now == pytest.approx(busy_polls)
    else:
        assert [argv for _, argv in ran] == [
            ("systemctl", "kill", "--signal=KILL", "alkera-orgs.slice")
        ]
        assert killed_at <= ran[0][0] < killed_at + 1.1


async def test_a_restart_in_place_starts_the_same_orgs_again_whatever_the_routing_reads(
    tmp_path: Path,
) -> None:
    """The routing reads a chat on a box that was restarting as asleep, so a
    supervisor that started workers only for chats the routing wants awake
    left every chat the restart kept (its lease held, its work owed) with no
    worker at all. The orgs the restart stopped are started again."""
    sup, box = await _draining(tmp_path, {"ALKERA_DAEMON_SUPERVISED": "1"}, ORG_A, ORG_B)
    box.ends_after = {ORG_A: 1.0, ORG_B: 1.0}
    await sup.drain(final=False)
    assert (tmp_path / "resume.json").exists()

    table = SlotTable(tmp_path / "slots.json")
    after = Box(table)
    nxt, _ = await _supervisor_on(after, table, tmp_path, {})
    nxt._resume = nxt._take_resume()
    asleep = [RouteEntry("chat-a", ORG_A, "asleep"), RouteEntry("chat-b", ORG_B, "asleep")]
    await nxt.reconcile(asleep, now=0)

    assert set(nxt.workers) == {ORG_A, ORG_B}
    # Each org leaves the list once its worker is serving, and not before.
    nxt._ready(ORG_A)
    assert (tmp_path / "resume.json").exists()
    nxt._ready(ORG_B)
    assert not (tmp_path / "resume.json").exists()


async def test_a_final_stop_leaves_nothing_to_resume(tmp_path: Path) -> None:
    (tmp_path / "resume.json").write_text(f'{{"orgs": ["{ORG_A}"]}}')
    sup, box = await _draining(tmp_path, {}, ORG_A)
    box.ends_after = {ORG_A: 1.0}
    await sup.drain(final=True)
    assert not (tmp_path / "resume.json").exists()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not-json"),
        pytest.param('{"orgs": ["not-an-org", 7]}', id="not-org-ids"),
        pytest.param('["a list"]', id="wrong-shape"),
    ],
)
async def test_a_resume_file_that_names_no_org_starts_nothing(tmp_path: Path, body: str) -> None:
    (tmp_path / "resume.json").write_text(body)
    sup, _box = await _draining(tmp_path, {})
    assert sup._take_resume() == set()
