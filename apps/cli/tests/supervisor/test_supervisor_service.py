"""The supervisor's loop: a worker per org with open chats, told only its
chats, stopped after a grace, restarted with a growing backoff."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.org_worker_protocol import (
    Credential,
    Route,
    Status,
    Stop,
    encode,
)
from alkera_cli.supervisor.http import ApiError
from alkera_cli.supervisor.org_credentials import RESEND_SECONDS
from alkera_cli.supervisor.service import (
    RESTART_BACKOFF_MIN,
    FileRoutingFeed,
    RouteEntry,
    Supervisor,
    Worker,
    parse_routing,
    worker_capacity,
)
from alkera_cli.supervisor.slots import SlotTable
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport

#: A box whose probe proved every mechanism but systemd: namespaced workers,
#: spawned directly.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})


ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


class Fleet:
    """Fake workers: what each was told, and whether it lives."""

    def __init__(self, table: SlotTable) -> None:
        self.table = table
        self.started: list[str] = []
        self.told: dict[str, list[object]] = {}
        self.dead: set[str] = set()
        self.refuse: set[str] = set()
        self.now = 0.0

    async def start(self, org_id: str) -> Worker:
        if org_id in self.refuse:
            raise RuntimeError("no namespace for you")
        self.started.append(org_id)
        slot = self.table.assign(org_id)
        told = self.told.setdefault(org_id, [])

        async def send(frame: Any) -> None:
            told.append(frame)

        return Worker(
            slot=slot, alive=lambda: org_id not in self.dead, send=send, started_at=self.now
        )


def _supervisor(tmp_path: Path, **kwargs: Any) -> tuple[Supervisor, Fleet]:
    table = SlotTable(tmp_path / "slots.json")
    fleet = Fleet(table)
    sup = Supervisor(
        api=None,  # type: ignore[arg-type]  # the loop under test never calls it
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=table,
        env={"ALKERA_ORG_WORKER_BUDGET": kwargs.pop("budget", "64")},
        start_worker=fleet.start,
        **{"isolation": NAMESPACED, **kwargs},
    )
    sup._clock = lambda: fleet.now
    return sup, fleet


def _routes(*pairs: tuple[str, str]) -> list[RouteEntry]:
    return [RouteEntry(chat, org) for chat, org in pairs]


async def test_each_org_gets_its_own_worker_told_only_its_own_chats(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B), ("a2", ORG_A)), now=0)
    assert sorted(fleet.started) == sorted([ORG_A, ORG_B])
    assert fleet.told[ORG_A] == [Route(chat_ids=("a1", "a2"))]
    assert fleet.told[ORG_B] == [Route(chat_ids=("b1",))]
    assert sup.workers[ORG_A].slot != sup.workers[ORG_B].slot


async def test_an_unchanged_routing_says_nothing_again(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    for now in (0, 3, 6):
        await sup.reconcile(_routes(("a1", ORG_A)), now=now)
    assert fleet.told[ORG_A] == [Route(chat_ids=("a1",))]
    assert fleet.started == [ORG_A]


async def test_an_org_whose_chats_all_sleep_gets_no_worker(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    await sup.reconcile([RouteEntry("a1", ORG_A, "asleep")], now=0)
    assert fleet.started == [] and not sup.workers


async def test_a_worker_is_told_every_chat_of_its_org_even_the_sleeping_ones(
    tmp_path: Path,
) -> None:
    """The worker decides which of its chats to hold; a chat that went back to
    sleep under it stays its own to put away, never someone else's."""
    sup, fleet = _supervisor(tmp_path)
    await sup.reconcile(
        [
            RouteEntry("a1", ORG_A),
            RouteEntry("a2", ORG_A, "asleep"),
            RouteEntry("b1", ORG_B, "asleep"),
        ],
        now=0,
    )
    assert fleet.started == [ORG_A]
    assert fleet.told[ORG_A] == [Route(chat_ids=("a1", "a2"))]


async def test_a_worker_still_holding_a_chat_is_not_stopped(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path, idle_grace_seconds=60)
    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    sup.workers[ORG_A].chats_served = 1
    asleep = [RouteEntry("a1", ORG_A, "asleep")]
    await sup.reconcile(asleep, now=10)
    await sup.reconcile(asleep, now=500)
    assert Stop() not in fleet.told[ORG_A]
    sup.workers[ORG_A].chats_served = 0
    await sup.reconcile(asleep, now=510)
    await sup.reconcile(asleep, now=570)
    assert fleet.told[ORG_A].count(Stop()) == 1


async def test_an_org_with_no_chat_left_is_told_then_stopped_after_the_grace(
    tmp_path: Path,
) -> None:
    sup, fleet = _supervisor(tmp_path, idle_grace_seconds=60)
    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    await sup.reconcile([], now=10)
    assert fleet.told[ORG_A][-1] == Route(chat_ids=())
    await sup.reconcile([], now=69)
    assert Stop() not in fleet.told[ORG_A]
    await sup.reconcile([], now=70)
    await sup.reconcile([], now=80)
    assert fleet.told[ORG_A].count(Stop()) == 1


async def test_a_chat_coming_back_inside_the_grace_keeps_the_worker(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path, idle_grace_seconds=60)
    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    await sup.reconcile([], now=10)
    await sup.reconcile(_routes(("a1", ORG_A)), now=50)
    await sup.reconcile([], now=100)
    assert Stop() not in fleet.told[ORG_A] and fleet.started == [ORG_A]


async def test_a_worker_that_keeps_exiting_waits_longer_each_time(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    routes = _routes(("a1", ORG_A))
    await sup.reconcile(routes, now=0)
    starts = [0.0]
    for _ in range(4):
        fleet.dead.add(ORG_A)
        fleet.now += 1
        await sup.reconcile(routes, now=fleet.now)  # notices the exit
        fleet.dead.discard(ORG_A)
        before = len(fleet.started)
        while len(fleet.started) == before:
            fleet.now += 0.5
            await sup.reconcile(routes, now=fleet.now)
        starts.append(fleet.now)
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert gaps == sorted(gaps) and gaps[-1] > gaps[0] >= RESTART_BACKOFF_MIN


async def test_an_org_whose_worker_cannot_start_does_not_hold_up_another(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    fleet.refuse.add(ORG_A)
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=0)
    assert fleet.started == [ORG_B] and ORG_A not in sup.workers


def test_routing_entries_with_a_malformed_org_are_dropped() -> None:
    entries = parse_routing(
        {
            "chats": [
                {"chat_id": "a1", "org_id": ORG_A},
                {"chat_id": "x", "org_id": "acme"},
                {"chat_id": 3, "org_id": ORG_A},
                "junk",
                {"chat_id": "b1", "org_id": ORG_B.upper(), "desired_state": "asleep"},
            ]
        }
    )
    assert entries == [RouteEntry("a1", ORG_A), RouteEntry("b1", ORG_B, "asleep")]


@pytest.mark.parametrize("payload", [{}, {"chats": "all"}, []])
def test_a_routing_with_no_chat_list_is_an_error_not_an_empty_box(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_routing(payload)


async def test_the_file_feed_reads_the_routing_and_an_absent_file_is_nothing(
    tmp_path: Path,
) -> None:
    feed = FileRoutingFeed(tmp_path / "routing.json")
    assert list(await feed.read()) == []
    (tmp_path / "routing.json").write_text(
        json.dumps({"chats": [{"chat_id": "a1", "org_id": ORG_A}]})
    )
    assert list(await feed.read()) == [RouteEntry("a1", ORG_A)]


class FakeApi:
    def __init__(self) -> None:
        self.minted: list[str] = []
        self.fail = False

    async def worker_credential(self, org_id: str) -> tuple[str, float]:
        if self.fail:
            raise ApiError(0, "down")
        self.minted.append(org_id)
        return f"alkm_org.{org_id}.{len(self.minted)}", 900.0


async def test_a_worker_gets_a_fresh_credential_before_its_own_runs_out(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    api = FakeApi()
    sup._api = api  # type: ignore[assignment]
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=0)
    sup.workers[ORG_A].credential.refresh_at = 100
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=99)
    assert not any(isinstance(f, Credential) for f in fleet.told[ORG_A])
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=100)
    sent = [f for f in fleet.told[ORG_A] if isinstance(f, Credential)]
    assert len(sent) == 1 and ORG_A in sent[0].credential
    # Each org's credential is minted for that org alone.
    assert api.minted == [ORG_A]
    assert not any(isinstance(f, Credential) for f in fleet.told[ORG_B])
    assert sup.workers[ORG_A].credential.refresh_at == pytest.approx(100 + 900 * 2 / 3)


async def test_a_refresh_that_fails_is_tried_again_soon(tmp_path: Path) -> None:
    sup, _fleet = _supervisor(tmp_path)
    api = FakeApi()
    api.fail = True
    sup._api = api  # type: ignore[assignment]
    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    sup.workers[ORG_A].credential.refresh_at = 10
    await sup.reconcile(_routes(("a1", ORG_A)), now=10)
    assert sup.workers[ORG_A].credential.refresh_at == 40


def _status(*, seq: int, refused: bool = False) -> Status:
    return Status(
        chats_served=0, chats_busy=0, rss_bytes=0, credential_seq=seq, credential_refused=refused
    )


async def test_a_credential_the_worker_never_took_is_sent_again_until_it_says_so(
    tmp_path: Path,
) -> None:
    """The first hand-off is lost (the worker never read it): the same
    credential goes out again once the worker's status is overdue to name it,
    with no second mint, and stops going out once a status names it."""
    sup, fleet = _supervisor(tmp_path)
    api = FakeApi()
    sup._api = api  # type: ignore[assignment]
    routes = _routes(("a1", ORG_A))
    await sup.reconcile(routes, now=0)
    sup.workers[ORG_A].credential.refresh_at = 100
    await sup.reconcile(routes, now=100)
    first = [f for f in fleet.told[ORG_A] if isinstance(f, Credential)]
    assert len(first) == 1
    fleet.told[ORG_A].clear()  # dropped on the way

    await sup.reconcile(routes, now=100 + RESEND_SECONDS - 1)
    assert not any(isinstance(f, Credential) for f in fleet.told[ORG_A])
    await sup.reconcile(routes, now=100 + RESEND_SECONDS)
    again = [f for f in fleet.told[ORG_A] if isinstance(f, Credential)]
    assert again == first
    assert api.minted == [ORG_A]

    reader = asyncio.StreamReader()
    reader.feed_data(encode(_status(seq=first[0].seq)))
    reader.feed_eof()
    await sup._listen(sup.workers[ORG_A], reader)
    fleet.told[ORG_A].clear()
    await sup.reconcile(routes, now=100 + 10 * RESEND_SECONDS)
    assert not any(isinstance(f, Credential) for f in fleet.told[ORG_A])


async def test_a_worker_that_asks_for_a_credential_is_handed_one_at_once(tmp_path: Path) -> None:
    """A worker whose status says its credential was refused as expired is
    minted and handed one on that status, not at the next scheduled refresh;
    a status saying the same again while that hand-off is pending mints
    nothing more."""
    sup, fleet = _supervisor(tmp_path)
    api = FakeApi()
    sup._api = api  # type: ignore[assignment]
    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    sup.workers[ORG_A].credential.refresh_at = 500
    reader = asyncio.StreamReader()
    reader.feed_data(encode(_status(seq=0, refused=True)))
    reader.feed_data(encode(_status(seq=0, refused=True)))
    reader.feed_eof()
    await sup._listen(sup.workers[ORG_A], reader)
    sent = [f for f in fleet.told[ORG_A] if isinstance(f, Credential)]
    assert len(sent) == 1 and api.minted == [ORG_A]


@pytest.mark.parametrize(
    ("entry", "state"),
    [
        pytest.param({"state": "asleep"}, "asleep", id="asleep"),
        pytest.param({"state": "awake"}, "open", id="awake"),
        pytest.param({"state": "working"}, "open", id="working"),
        pytest.param({"state": "asleep", "pending_turn": True}, "open", id="asleep-but-owed"),
        pytest.param({"state": "asleep", "wake_requested": True}, "open", id="asleep-but-woken"),
    ],
)
def test_the_backend_routing_says_which_chats_to_hold(entry: dict[str, Any], state: str) -> None:
    payload = {"items": [{"chat_id": "a1", "org_id": ORG_A, **entry}], "next_cursor": None}
    assert parse_routing(payload) == [RouteEntry("a1", ORG_A, state)]


async def test_no_new_org_starts_past_the_worker_budget(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path, budget="1")
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=0)
    assert len(fleet.started) == 1
    # The org already running keeps its worker; the other waits for a slot.
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B), ("a2", ORG_A)), now=5)
    assert len(fleet.started) == 1


async def test_the_heartbeat_reports_workers_their_memory_and_the_budget(tmp_path: Path) -> None:
    sup, _fleet = _supervisor(tmp_path, budget="7")
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=0)
    sup.workers[ORG_A].rss_bytes = 300 << 20
    sup.workers[ORG_B].rss_bytes = 200 << 20
    resources = sup.resources()
    assert (resources["org_workers"], resources["org_worker_capacity"]) == (2, 7)
    assert resources["org_worker_memory_bytes"] == 500 << 20


async def test_what_the_supervisor_sends_is_what_the_backend_reads(tmp_path: Path) -> None:
    """The supervisor builds its bodies without the backend's schema package
    (which would load the database layer into the box's root process); the
    backend's own models must read them unchanged."""
    from alkera_cli.supervisor.service import MACHINE_CREDENTIAL_HEADER
    from alkera_core.auth import machine_token
    from alkera_core.schemas.compute import MachineClaimRequest, MachineHeartbeatRequest

    sup, _fleet = _supervisor(tmp_path)
    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    claim = sup.claim_body()
    claim["provider_pod_id"] = "i-0123"
    assert MachineClaimRequest.model_validate(claim).model_dump(include=set(claim)) == claim
    beat = sup.heartbeat_body()
    read = MachineHeartbeatRequest.model_validate(beat)
    assert read.resources is not None
    sent = beat["resources"]
    assert isinstance(sent, dict)
    assert read.resources.model_dump(include=set(sent)) == sent
    assert read.capabilities == beat["capabilities"]
    assert MACHINE_CREDENTIAL_HEADER == machine_token.MACHINE_CREDENTIAL_HEADER


@pytest.mark.parametrize(
    ("env", "total_gb", "capacity"),
    [
        pytest.param({}, 32, 20, id="a-quarter-of-32gb-at-400mb"),
        pytest.param({"ALKERA_ORG_WORKER_MEMORY_MB": "1024"}, 32, 8, id="bigger-workers"),
        pytest.param({"ALKERA_ORG_WORKER_BUDGET": "3"}, 32, 3, id="named-budget"),
        pytest.param({"ALKERA_ORG_WORKER_BUDGET": "0"}, 32, 20, id="zero-is-not-a-budget"),
        pytest.param({}, 0, 1, id="unknown-memory-still-one"),
    ],
)
def test_the_worker_budget(env: dict[str, str], total_gb: int, capacity: int) -> None:
    assert worker_capacity(env, memory_total_bytes=total_gb << 30) == capacity


async def test_the_beat_names_a_crash_looping_org_until_its_chats_leave(tmp_path: Path) -> None:
    """Placement sends the named org elsewhere; once none of its chats is
    routed here the box stops naming it, so it may be given the org again.
    Another org that never failed is never named."""
    sup, fleet = _supervisor(tmp_path)
    fleet.refuse.add(ORG_A)
    routes = _routes(("a1", ORG_A), ("b1", ORG_B))
    for now in (0.0, 1000.0, 2000.0):
        await sup.reconcile(routes, now=now)
    named = sup.heartbeat_body()["resources"]
    assert isinstance(named, dict)
    assert named["org_workers_failing_ids"] == [ORG_A]
    await sup.reconcile(_routes(("b1", ORG_B)), now=3000.0)
    named = sup.heartbeat_body()["resources"]
    assert isinstance(named, dict)
    assert "org_workers_failing_ids" not in named


GIB = 1 << 30


@pytest.mark.parametrize(
    ("env", "total_gb", "orgs", "share"),
    [
        pytest.param({}, 32, 1, 31 * GIB, id="one-org-gets-all-but-the-reserve"),
        pytest.param({}, 32, 2, 31 * GIB // 2, id="two-orgs-split-it"),
        pytest.param({}, 32, 0, 31 * GIB, id="no-org-yet-counts-as-one"),
        pytest.param({}, 8, 20, GIB, id="never-under-the-floor"),
        pytest.param({}, 1, 1, GIB, id="a-box-no-bigger-than-the-reserve-still-gets-the-floor"),
        pytest.param({"ALKERA_ORG_WORKER_BUDGET": "4"}, 32, 1, 8 * GIB, id="a-named-budget"),
        pytest.param({"ALKERA_ORG_WORKER_BUDGET": "0"}, 32, 2, 31 * GIB // 2, id="zero-budget"),
        pytest.param({}, 0, 3, 0, id="unknown-memory-sets-no-limit"),
    ],
)
def test_each_org_gets_its_share_of_the_boxs_memory(
    env: dict[str, str], total_gb: int, orgs: int, share: int
) -> None:
    """The box's memory less its reserve, split over the orgs it serves now,
    so one busy org is held to its share instead of taking the others'."""
    from alkera_cli.supervisor.org_capacity import org_memory_share

    assert org_memory_share(env, memory_total_bytes=total_gb * GIB, orgs=orgs) == share


async def test_every_running_org_follows_the_share_as_orgs_come_and_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second org halves the first one's limit; when it leaves, the first
    gets the box back. Set on the unit on systemd."""
    from alkera_cli.supervisor import service
    from alkera_cli.supervisor.launch import WorkerLaunch

    monkeypatch.setattr(service.host_resources, "effective_memory_bytes", lambda: 33 * GIB)
    sup, fleet = _supervisor(tmp_path, isolation=IsolationReport(frozenset(IsolationMechanism)))
    start = fleet.start

    async def with_launch(org_id: str) -> Worker:
        worker = await start(org_id)
        worker.launch = WorkerLaunch(slot=worker.slot, org_root=tmp_path, argv=("w",))
        return worker

    sup._start = with_launch
    sup._env.pop("ALKERA_ORG_WORKER_BUDGET")
    ran: list[tuple[str, ...]] = []
    sup._runner = lambda argv: ran.append(tuple(argv)) or 0

    def limits() -> dict[str, str]:
        found = {a[3]: a[4] for a in ran if a[:3] == ("systemctl", "set-property", "--runtime")}
        ran.clear()
        return found

    await sup.reconcile(_routes(("a1", ORG_A)), now=0)
    unit_a = sup.workers[ORG_A].launch.unit  # type: ignore[union-attr]
    assert limits() == {unit_a: f"MemoryMax={32 * GIB}"}
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=1)
    unit_b = sup.workers[ORG_B].launch.unit  # type: ignore[union-attr]
    assert limits() == {unit_a: f"MemoryMax={16 * GIB}", unit_b: f"MemoryMax={16 * GIB}"}
    await sup.reconcile(_routes(("a1", ORG_A), ("b1", ORG_B)), now=2)
    assert limits() == {}
    fleet.dead.add(ORG_B)
    sup.workers[ORG_B].stopping = True
    await sup.reconcile(_routes(("a1", ORG_A)), now=3)
    assert limits() == {unit_a: f"MemoryMax={32 * GIB}"}
