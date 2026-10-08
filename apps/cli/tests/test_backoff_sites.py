"""Each site that backs off keeps its own schedule.

The CLI computes every growing wait through one owner
(:mod:`alkera_cli.host.backoff`); each caller keeps its own first wait, growth
and ceiling. These tables are what each caller waited before the owner
existed, driven through the caller itself, so a change of formula at any one
of them shows up here as a changed number rather than a quieter or louder
retry in production.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from alkera_cli.files.inbound_backoff import InboundBackoff
from alkera_cli.files.round_retry import RoundRetry
from alkera_cli.harness.adapters.opencode_http import start_retry_delays
from alkera_cli.host import paths
from alkera_core.project import FileLock, LockHeldError

# --- the inbound download refusal streak --------------------------------------


def _inbound_waits(refusals: int) -> list[float]:
    """The wait each refusal in a streak earned, read through ``waiting``."""
    backoff = InboundBackoff()
    waits: list[float] = []
    now = 0.0
    for _ in range(refusals):
        backoff.refused("node", 1, now)
        wait = 0.0
        # The first whole second at which the node may be asked again.
        while backoff.waiting("node", 1, now + wait):
            wait += 1.0
        waits.append(wait)
        now += wait
    return waits


def test_an_inbound_refusal_streak_doubles_from_two_seconds_to_ten_minutes() -> None:
    assert _inbound_waits(11) == [2, 4, 8, 16, 32, 64, 128, 256, 512, 600, 600]


def test_a_newer_entry_or_a_landed_download_starts_the_streak_over() -> None:
    backoff = InboundBackoff()
    assert backoff.refused("node", 1, 0.0) is True
    assert backoff.refused("node", 1, 2.0) is False
    assert backoff.waiting("node", 1, 5.0)
    assert not backoff.waiting("node", 2, 5.0), "a newer entry is asked for at once"
    assert backoff.refused("node", 2, 5.0) is True
    assert not backoff.waiting("node", 2, 7.0), "a new streak starts at the first wait"
    backoff.landed("node")
    assert backoff.refused("node", 2, 7.0) is True


# --- the live sync round retry ----------------------------------------------


def test_a_refused_round_doubles_from_one_second_to_thirty() -> None:
    retry = RoundRetry()
    refused = httpx.HTTPStatusError(
        "no", request=httpx.Request("GET", "http://x"), response=httpx.Response(503)
    )
    waits = []
    for second in range(8):
        retry.failed(refused, float(second))
        waits.append(retry.wait)
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_a_round_nothing_answered_retries_fast_then_joins_the_doubling() -> None:
    retry = RoundRetry()
    unanswered = httpx.ConnectError("reset")
    waits = [
        (retry.failed(unanswered, at), retry.wait)[1] for at in (0.0, 0.5, 14.9, 15.0, 16.0, 18.0)
    ]
    assert waits == [0.5, 0.5, 0.5, 1.0, 2.0, 4.0]
    retry.landed()
    assert retry.failed(unanswered, 20.0) is True
    assert retry.wait == 0.5, "a landed round starts the next streak over"


# --- the opencode whole-start retries ----------------------------------------


def test_an_unbounded_opencode_start_doubles_from_one_second_to_a_minute() -> None:
    assert start_retry_delays(None)[:9] == (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0)
    assert len(start_retry_delays(None)) == 64


# --- the preferences and instructions file locks ------------------------------


class _Time:
    """A wall that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(
    ("module_name", "lock_class", "path_name"),
    [
        pytest.param(
            "alkera_cli.preferences.user",
            "_PreferencesLock",
            "PREFERENCES_LOCK_PATH",
            id="preferences",
        ),
        pytest.param(
            "alkera_cli.preferences.instructions",
            "_InstructionsLock",
            "INSTRUCTIONS_LOCK_PATH",
            id="instructions",
        ),
    ],
)
def test_a_held_settings_lock_is_polled_from_twenty_ms_to_two_hundred_until_two_seconds(
    module_name: str,
    lock_class: str,
    path_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import importlib

    module = importlib.import_module(module_name)
    lock_path = tmp_path / "held.lock"
    monkeypatch.setattr(paths, path_name, lock_path)
    clock = _Time()
    monkeypatch.setattr(
        module, "time", SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic)
    )
    holder = FileLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(LockHeldError), getattr(module, lock_class)():
            pytest.fail("acquired a lock another holder has")
    finally:
        holder.release()
    head = [0.02, 0.04, 0.08, 0.16]
    assert clock.slept[:4] == pytest.approx(head)
    assert set(clock.slept[4:]) == {0.2}
    # Gave up on the first poll at or past the two-second deadline.
    assert sum(clock.slept[:-1]) < 2.0 <= sum(clock.slept)


def test_a_settings_lock_freed_while_waiting_is_taken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from alkera_cli.preferences import user

    lock_path = tmp_path / "freed.lock"
    monkeypatch.setattr(paths, "PREFERENCES_LOCK_PATH", lock_path)
    holder = FileLock(lock_path)
    holder.acquire()
    clock = _Time()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.slept) == 3:
            holder.release()

    monkeypatch.setattr(user, "time", SimpleNamespace(sleep=sleep, monotonic=clock.monotonic))
    with user._PreferencesLock() as taken:
        assert taken is not holder
    assert clock.slept == pytest.approx([0.02, 0.04, 0.08])


# --- the sync's Retry-After -------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("4", 4.0, id="seconds"),
        pytest.param("2.5", 2.5, id="fractional-seconds"),
        pytest.param("-5", None, id="a-negative-wait-is-not-a-wait"),
        pytest.param("Wed, 21 Oct 2026 07:28:00 GMT", None, id="a-date-is-left-to-the-backoff"),
        pytest.param(None, None, id="absent"),
    ],
)
def test_a_live_sync_refusal_reads_only_a_seconds_retry_after(
    header: str | None, expected: float | None
) -> None:
    from alkera_cli.host.backoff import retry_after_of

    headers = {} if header is None else {"Retry-After": header}
    refusal = httpx.HTTPStatusError(
        "slow down",
        request=httpx.Request("PUT", "http://drive"),
        response=httpx.Response(429, headers=headers),
    )
    assert retry_after_of(refusal) == expected
    assert retry_after_of(RuntimeError("no response")) is None


# --- the box supervisor's worker restarts ------------------------------------------


class _Fleet:
    """Org workers that refuse to start, or start and die, on cue."""

    def __init__(self, table: Any) -> None:
        self.table = table
        self.asked: list[float] = []
        self.refuse = True
        self.dead = False
        self.now = 0.0

    async def start(self, org_id: str) -> Any:
        from alkera_cli.supervisor.service import Worker

        self.asked.append(self.now)
        if self.refuse:
            raise RuntimeError("no namespace for you")

        async def send(_frame: object) -> None:
            return None

        return Worker(
            slot=self.table.assign(org_id),
            alive=lambda: not self.dead,
            send=send,
            started_at=self.now,
        )


_ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"


def _supervisor(tmp_path: Path) -> tuple[Any, _Fleet]:
    from alkera_cli.supervisor.service import FileRoutingFeed, Supervisor
    from alkera_cli.supervisor.slots import SlotTable
    from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport

    table = SlotTable(tmp_path / "slots.json")
    fleet = _Fleet(table)
    sup = Supervisor(
        api=None,
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        isolation=IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD}),
        slots=table,
        env={"ALKERA_ORG_WORKER_BUDGET": "8"},
        start_worker=fleet.start,
    )
    sup._clock = lambda: fleet.now
    return sup, fleet


def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in itertools.pairwise(times)]


async def test_a_worker_that_cannot_start_is_retried_from_two_seconds_doubling_to_two_minutes(
    tmp_path: Path,
) -> None:
    from alkera_cli.supervisor.service import RouteEntry

    sup, fleet = _supervisor(tmp_path)
    for second in range(500):
        fleet.now = float(second)
        await sup.reconcile([RouteEntry("a1", _ORG)], now=fleet.now)
    assert _gaps(fleet.asked)[:8] == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 120.0, 120.0]


async def test_a_worker_that_keeps_exiting_waits_longer_until_it_runs_a_minute(
    tmp_path: Path,
) -> None:
    from alkera_cli.supervisor.service import RouteEntry

    sup, fleet = _supervisor(tmp_path)
    fleet.refuse = False
    routes = [RouteEntry("a1", _ORG)]
    lives = [1, 1, 1, 1, 1, 1, 1, 1, 61, 1]
    for life in lives:
        before = len(fleet.asked)
        while len(fleet.asked) == before:
            await sup.reconcile(routes, now=fleet.now)
            if len(fleet.asked) == before:
                fleet.now += 1.0
        fleet.dead = False
        fleet.now += life
        fleet.dead = True
        await sup.reconcile(routes, now=fleet.now)  # notices the exit
        fleet.dead = False
    exits = [start + life for start, life in zip(fleet.asked, lives, strict=False)]
    waits = [start - exited for exited, start in zip(exits, fleet.asked[1:], strict=False)]
    # A worker that ran past a minute before it exited starts again at the floor.
    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 120.0, 120.0, 2.0]


# --- the box log shipper -----------------------------------------------------------


class _FlakySink:
    """A sink that refuses the sends named in ``refused`` (counted from one)."""

    max_events = 100
    max_bytes = 1 << 20

    def __init__(self, refused: set[int]) -> None:
        self.refused = refused
        self.sends = 0

    def send(self, records: Any) -> None:
        self.sends += 1
        if self.sends in self.refused:
            raise OSError("the backend is away")


class _CountedStop:
    """The shipper's stop event: hands out each wait, and stops after ``n``."""

    def __init__(self, n: int, refill: Any) -> None:
        self.waits: list[float] = []
        self.n = n
        self.refill = refill

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.refill()
        return len(self.waits) > self.n

    def set(self) -> None:
        return None


def test_the_log_shipper_doubles_its_wait_after_a_refused_send_and_resets_on_one_that_lands() -> (
    None
):
    from alkera_cli.supervisor.box_log_shipping import Ring, Shipper

    ring = Ring()
    sink = _FlakySink(refused={1, 2, 3, 4, 5, 6, 8})
    shipper = Shipper(ring, sink, flush_seconds=1.0, backoff_max=10.0)
    stop = _CountedStop(9, refill=lambda: ring.push({"event": "box.tick", "level": "info"}))
    shipper._stop = stop
    shipper._run()
    assert stop.waits == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 1.0, 2.0, 1.0]


# --- the draining box's sandbox reads ------------------------------------------------


class _Busy:
    """A sandbox that always holds a background process, counting its reads."""

    def __init__(self, clock: Any) -> None:
        self.read_at: list[float] = []
        self.clock = clock

    def work(self, session_id: str) -> Any:
        from alkera_cli.harness.sandbox_processes import SandboxProcess

        self.read_at.append(self.clock())
        return (SandboxProcess(pid=7, ppid=1, command="sleep 9999"),)

    def drop(self, session_id: str) -> None:
        return None


async def test_a_draining_box_rereads_a_held_sandbox_on_a_doubling_interval_to_a_minute() -> None:
    from alkera_cli.cloud.sleep_policy import SleepPolicy, SleepSettings

    now = [0.0]
    probes = _Busy(lambda: now[0])
    policy = SleepPolicy(
        idle_minutes=60.0,
        parked_ask_hours=24.0,
        clock=lambda: now[0],
        memory=lambda: None,
        probes=probes,
        settings=SleepSettings(drain_process_hold_seconds=10_000.0),
    )
    for second in range(400):
        now[0] = float(second)
        assert await policy.drain_releasable(["chat-1"]) == []
    assert _gaps(probes.read_at)[:7] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


# --- the live sync's throttles and metadata refusals -------------------------------------


@pytest.mark.parametrize(
    ("attempt", "header", "expected"),
    [
        pytest.param(0, None, 1.0, id="first-unnamed-wait"),
        pytest.param(1, None, 2.0, id="doubles"),
        pytest.param(4, None, 16.0, id="still-under-the-ceiling"),
        pytest.param(5, None, 30.0, id="capped-at-thirty"),
        pytest.param(9, None, 30.0, id="stays-capped"),
        pytest.param(0, "12", 12.0, id="a-named-wait-is-obeyed"),
        pytest.param(0, "900", 30.0, id="a-named-wait-past-the-ceiling-is-capped"),
        pytest.param(3, "-5", 8.0, id="a-negative-name-falls-back-to-the-doubling"),
    ],
)
def test_a_throttled_live_sync_call_waits_the_servers_word_or_a_doubling(
    attempt: int, header: str | None, expected: float
) -> None:
    from alkera_cli.files.live_sync import throttle_wait

    headers = {} if header is None else {"Retry-After": header}
    refusal = httpx.HTTPStatusError(
        "slow down",
        request=httpx.Request("PUT", "http://drive"),
        response=httpx.Response(429, headers=headers),
    )
    assert throttle_wait(refusal, attempt) == expected


def test_a_refused_call_that_is_no_throttle_gets_no_throttle_wait() -> None:
    from alkera_cli.files.live_sync import throttle_wait

    refusal = httpx.HTTPStatusError(
        "no", request=httpx.Request("PUT", "http://drive"), response=httpx.Response(409)
    )
    assert throttle_wait(refusal, 0) is None


def test_refused_metadata_waits_from_one_second_doubling_to_thirty(tmp_path: Path) -> None:
    from files._live_sync_fakes import FakeClock, FakeLiveApi, make_sync

    root = tmp_path / "scratch"
    root.mkdir()
    sync = make_sync(root, FakeLiveApi(root=root), FakeClock())
    waits = []
    for _ in range(7):
        sync._meta_backoff(RuntimeError("the drive is between deploys"))
        waits.append(sync._meta_wait)
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


# --- the box's folder push after a turn ----------------------------------------------


async def test_a_folder_push_that_does_not_land_waits_a_minute_doubling_to_half_an_hour() -> None:
    from alkera_cli.cloud.service import CloudMirrorService

    now = [0.0]
    pushed_at: list[float] = []

    class _Folders:
        def push(self, chat_id: str) -> None:
            pushed_at.append(now[0])

    box = object.__new__(CloudMirrorService)
    box._mirrors = {"chat-1": SimpleNamespace(turn_running=False, published_count=3)}
    box._pushed_at = {}
    box._push_retry = {}
    box._clock = lambda: now[0]
    box._folders = _Folders()
    for second in range(0, 8000, 10):
        now[0] = float(second)
        await box._push_folder_if_due("chat-1")
    assert _gaps(pushed_at)[:7] == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]


# --- a notebook kernel's events on their way to the backend ---------------------------


async def test_a_refused_batch_of_kernel_events_waits_half_a_second_then_doubles() -> None:
    from alkera_cli.notebooks.box_events import EventsNotDeliveredError, HttpKernelEvents

    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    busy = httpx.MockTransport(lambda _request: httpx.Response(503))
    async with httpx.AsyncClient(transport=busy, base_url="http://api") as http:
        events = HttpKernelEvents(http=http, attempts=5, sleep=sleep)
        where: Any = SimpleNamespace(drive_id="d1", item_id="i1")
        with pytest.raises(EventsNotDeliveredError):
            await events.post(where, kernel_id="k1", state=None, events=[{"seq": 1}])
    assert slept == [0.5, 1.0, 2.0, 4.0]
