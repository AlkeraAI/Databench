"""A box told to stop finishes what it holds and takes nothing new.

A deploy that cuts a box mid-turn throws away an answer a reader is waiting on
and leaves the chat's last files only on that box's disk. So a box drains: the
moment it is signalled it stops accepting chats and says so on its heartbeat
(which is what takes it out of the allocator), hands back everything that owes
nothing so another box can pick it up at once, and waits for the turns still
running.

The wait has a ceiling, and the ceiling is not a limit on how long an answer
may take — a turn may legitimately run for hours. It is what makes a deploy
finishable by an operator: without it one wedged chat holds the box for ever
and the only move left is the kill this whole path exists to avoid.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _mirror_service import SERVICE_LOGGER, Clock, build_service
from alkera_cli.cloud.org_worker import serve_control
from alkera_cli.cloud.service import (
    DEFAULT_DRAIN_CEILING_SECONDS,
    CloudMirrorService,
    drain_ceiling_from_env,
    supervision_from_env,
)
from alkera_cli.org_worker_protocol import Stop, encode
from alkera_core.compute.bootstrap import FINAL_STOP_FILE, BootstrapSpec, render_bootstrap
from alkera_core.compute.box_contract import BootstrapPlan, StartMode
from alkera_core.compute.liveness import (
    DRAIN_CEILING_SECONDS,
    ENV_DRAIN_CEILING_SECONDS,
    RESTART_DRAIN_CEILING_SECONDS,
)

#: What every render here installs and starts, unless a test says otherwise.
PLAN = BootstrapPlan(version="1.4.2", start_mode=StartMode.SUPERVISE)

# -- the ceiling is a setting -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, DEFAULT_DRAIN_CEILING_SECONDS, id="unset"),
        pytest.param("", DEFAULT_DRAIN_CEILING_SECONDS, id="blank"),
        pytest.param("   ", DEFAULT_DRAIN_CEILING_SECONDS, id="whitespace"),
        pytest.param("nope", DEFAULT_DRAIN_CEILING_SECONDS, id="not-a-number"),
        pytest.param("inf", DEFAULT_DRAIN_CEILING_SECONDS, id="infinity"),
        pytest.param("1e999", DEFAULT_DRAIN_CEILING_SECONDS, id="overflows-to-infinity"),
        pytest.param("-30", DEFAULT_DRAIN_CEILING_SECONDS, id="negative"),
        pytest.param("0", 0.0, id="zero-means-hand-back-at-once"),
        pytest.param("900", 900.0, id="a-ceiling-an-operator-chose"),
    ],
)
def test_the_drain_ceiling_is_a_setting_with_a_generous_default(
    raw: str | None, expected: float
) -> None:
    """A typo in a deploy's environment must not silently turn a drain into a
    cut, so anything unusable reads as the default rather than as zero. ``0``
    is the one way to say "hand everything back now", so it is kept."""
    env = {} if raw is None else {ENV_DRAIN_CEILING_SECONDS: raw}
    assert drain_ceiling_from_env(env) == expected


def test_the_box_and_the_platform_wait_by_the_same_number() -> None:
    """An operator plans a deploy by this number and the box waits by it. Two
    spellings drift, and the drift is invisible from either side."""
    assert DEFAULT_DRAIN_CEILING_SECONDS == float(DRAIN_CEILING_SECONDS)


# -- the flip -----------------------------------------------------------------


def _chat(chat_id: str) -> dict[str, Any]:
    return {"id": chat_id, "machine_id": "machine:x", "last_seq": 1}


async def test_a_box_that_was_told_to_stop_says_so_before_it_waits(tmp_path: Path) -> None:
    """The beat that says "draining" is what stops the allocator placing a
    chat here, so the flip cannot wait for the first pass of the drain: a chat
    landing in between would be one this process has to hand straight on."""
    service, _built = build_service(tmp_path, clock=Clock())
    assert service.draining is False
    service.begin_drain()
    assert service.draining is True


async def test_the_heartbeat_carries_the_drain(tmp_path: Path) -> None:
    """What the platform is told — the whole mechanism the allocator reads."""
    said: list[dict[str, Any]] = []

    beaten = asyncio.Event()

    async def _beat(machine_id: str, **kwargs: Any) -> dict[str, Any]:
        said.append(kwargs)
        beaten.set()
        return {}

    service, _built = build_service(tmp_path, clock=Clock())
    service._machine_id = "machine:x"
    service._rest.heartbeat_machine = _beat  # type: ignore[method-assign]

    async def _one_beat() -> None:
        beaten.clear()
        task = asyncio.ensure_future(service._machine_loop())
        await asyncio.wait_for(beaten.wait(), timeout=10.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    await _one_beat()
    assert said[-1]["draining"] is False

    said.clear()
    service.begin_drain()
    await _one_beat()
    assert said[-1]["draining"] is True


async def test_a_draining_box_takes_no_new_chat(tmp_path: Path) -> None:
    """A chat the backend has not yet moved off this box must not be opened
    here: the session would be spawned to be torn down a moment later, on a
    lease the next box then has to wait out."""
    service, _built = build_service(tmp_path, clock=Clock())
    service._machine_id = "machine:x"
    assert service._serves(_chat("chat-new")) is True

    service.begin_drain()
    assert service._serves(_chat("chat-new")) is False


async def test_a_draining_box_keeps_serving_the_chat_it_already_holds(tmp_path: Path) -> None:
    """The point of draining rather than stopping: the answer already in
    flight is finished, not dropped."""
    service, _built = build_service(tmp_path, clock=Clock())
    service._machine_id = "machine:x"
    await service._ensure_mirror("chat-held", _chat("chat-held"))

    service.begin_drain()
    assert service._serves(_chat("chat-held")) is True
    assert service._serves(_chat("chat-other")) is False


# -- what the drain does with each chat ---------------------------------------


async def test_every_quiet_chat_is_handed_back_at_once(tmp_path: Path) -> None:
    """They owe nothing, so another box can pick them up while this one is
    still finishing the rest — that IS the hand-over."""
    service, built = build_service(tmp_path, clock=Clock())
    for chat_id in ("chat-a", "chat-b"):
        await service._ensure_mirror(chat_id, _chat(chat_id))

    await service.drain()

    assert service.mirrors == {}
    assert built["chat-a"].stopped and built["chat-b"].stopped


async def test_a_running_turn_is_waited_for_and_released_when_it_ends(tmp_path: Path) -> None:
    """The whole reason a box drains. The busy chat is not touched while its
    turn runs, and is handed back the moment it ends."""
    clock = Clock()
    waits = 0

    async def _sleep(seconds: float) -> None:
        nonlocal waits
        waits += 1
        clock.advance(seconds)
        if waits == 3:
            built["chat-busy"].turn_running = False
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep)
    for chat_id in ("chat-busy", "chat-quiet"):
        await service._ensure_mirror(chat_id, _chat(chat_id))
    built["chat-busy"].turn_running = True

    await service.drain()

    assert waits >= 3, "the drain returned without ever waiting on the running turn"
    assert built["chat-quiet"].stopped, "the quiet chat should not wait for the busy one"
    assert built["chat-busy"].stopped
    assert service.mirrors == {}


async def test_a_turn_that_never_ends_is_handed_back_at_the_ceiling(tmp_path: Path) -> None:
    """Without the ceiling one wedged chat holds the box for ever and the
    operator's only remaining move is the kill. What is still running is handed
    back where it stands, so the next box resumes it."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, drain_ceiling_seconds=30.0)
    await service._ensure_mirror("chat-wedged", _chat("chat-wedged"))
    built["chat-wedged"].turn_running = True

    started = clock.now
    await asyncio.wait_for(service.drain(), timeout=10.0)

    assert clock.now - started >= 30.0, "the drain gave up before its ceiling"
    assert not built["chat-wedged"].stopped, "the wedged turn was cut short by the drain itself"
    assert service.mirrors == {"chat-wedged": built["chat-wedged"]}


@pytest.mark.parametrize(
    ("parked_only", "waits_for_the_ceiling"),
    [
        pytest.param(True, False, id="only-an-unanswered-ask"),
        pytest.param(False, True, id="work-still-running-under-the-ask"),
    ],
)
async def test_a_chat_parked_on_an_unanswered_ask_does_not_hold_the_stop(
    tmp_path: Path, parked_only: bool, waits_for_the_ceiling: bool
) -> None:
    """A chat whose only work is an unanswered permission card must not hold a
    node's stop for the full six-hour ceiling. The ask is
    durable — it stays in the transcript and the next open re-offers it — so
    the chat is handed back at once like an idle one. A chat with a subagent
    or tool still running under the ask is real work and is still waited for."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, drain_ceiling_seconds=600.0)
    await service._ensure_mirror("chat-asking", _chat("chat-asking"))
    asking = built["chat-asking"]
    asking.turn_running = True
    asking.waiting_on_a_person = True
    asking.parked_only = parked_only

    started = clock.now
    await asyncio.wait_for(service.drain(), timeout=10.0)

    if waits_for_the_ceiling:
        assert clock.now - started >= 600.0
        assert service.mirrors == {"chat-asking": asking}
    else:
        assert clock.now - started == 0.0, "the stop waited on a person who may never answer"
        assert asking.stopped
        assert service.mirrors == {}


async def test_a_supervised_restart_does_not_wait_on_an_unanswered_ask(tmp_path: Path) -> None:
    """The restart keeps the chat either way, and the next process re-offers
    the ask; waiting its short ceiling for a person only lengthens the gap."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, supervised=True)
    await service._ensure_mirror("chat-asking", _chat("chat-asking"))
    built["chat-asking"].turn_running = True
    built["chat-asking"].parked_only = True

    started = clock.now
    await asyncio.wait_for(service.drain(), timeout=10.0)

    assert clock.now - started == 0.0
    assert service.mirrors == {"chat-asking": built["chat-asking"]}


async def test_a_zero_ceiling_hands_everything_back_without_waiting(tmp_path: Path) -> None:
    """The deploy that cannot wait. Still a hand-back, never a cut: the chats
    are released the same way, so they resume on the next box."""
    clock = Clock()
    waited = False

    async def _sleep(seconds: float) -> None:
        nonlocal waited
        waited = True
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, drain_ceiling_seconds=0.0)
    for chat_id in ("chat-busy", "chat-quiet"):
        await service._ensure_mirror(chat_id, _chat(chat_id))
    built["chat-busy"].turn_running = True

    await service.drain()

    assert waited is False
    assert built["chat-quiet"].stopped
    assert service.mirrors == {"chat-busy": built["chat-busy"]}


async def test_the_ceiling_a_caller_names_wins_over_the_setting(tmp_path: Path) -> None:
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(
        tmp_path, clock=clock, sleep=_sleep, drain_ceiling_seconds=10_000.0
    )
    await service._ensure_mirror("chat-wedged", _chat("chat-wedged"))
    built["chat-wedged"].turn_running = True

    started = clock.now
    await asyncio.wait_for(service.drain(5.0), timeout=10.0)
    assert clock.now - started < 100.0


async def test_the_drain_says_what_it_gave_up_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator watching a deploy has to be able to tell "everything
    finished" from "one chat was still running when the ceiling came"."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, drain_ceiling_seconds=5.0)
    await service._ensure_mirror("chat-wedged", _chat("chat-wedged"))
    built["chat-wedged"].turn_running = True

    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        await service.drain()

    assert any("still running after" in record.getMessage() for record in caplog.records)


async def test_giving_up_on_the_drain_never_cuts_a_hand_back_in_half(tmp_path: Path) -> None:
    """A release takes the chat off this box BEFORE it pushes its folder, so a
    cancellation landing in the middle would leave the chat with neither a box
    nor its last turn's files. The wait ends; the hand-back in flight finishes
    and the cancellation is re-raised after it."""
    clock = Clock()
    stopping = asyncio.Event()

    service, built = build_service(tmp_path, clock=clock)
    await service._ensure_mirror("chat-slow", _chat("chat-slow"))
    slow = built["chat-slow"]
    real_stop = slow.stop

    async def _slow_stop() -> None:
        stopping.set()
        await asyncio.sleep(0.2)
        await real_stop()

    slow.stop = _slow_stop  # type: ignore[method-assign]

    draining = asyncio.ensure_future(service.drain())
    await asyncio.wait_for(stopping.wait(), timeout=10.0)
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(draining, timeout=10.0)

    assert slow.stopped, "the hand-back was cut off part-way by the cancellation"
    assert service.mirrors == {}


# -- where the drain sits in the box's life -----------------------------------


async def _drained(service: CloudMirrorService) -> bool:
    return service.draining


async def test_a_box_asked_to_stop_drains_before_it_stops(tmp_path: Path) -> None:
    clock = Clock()
    service, _built = build_service(tmp_path, clock=clock)
    drained: list[float] = []
    real_drain = service.drain

    async def _drain(ceiling: float | None = None) -> None:
        drained.append(clock.now)
        await real_drain(ceiling)

    service.drain = _drain  # type: ignore[method-assign]
    service.require_harness = lambda: None  # type: ignore[method-assign]
    service.require_machine_identity = lambda: None  # type: ignore[assignment]
    service.refresh_sources = lambda: None  # type: ignore[method-assign]

    async def _register() -> bool:
        return False

    service._register = _register  # type: ignore[method-assign]

    stop = asyncio.Event()
    running = asyncio.ensure_future(service.run_until(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(running, timeout=10.0)

    assert drained, "a box asked to stop went straight to the stop without draining"


async def test_a_box_that_gave_up_does_not_drain(tmp_path: Path) -> None:
    """A box whose credential was refused cannot publish another token, so
    waiting on its turns only delays handing the chats to a box that can."""
    clock = Clock()
    service, _built = build_service(tmp_path, clock=clock)
    drained: list[float] = []

    async def _drain(ceiling: float | None = None) -> None:
        drained.append(clock.now)

    service.drain = _drain  # type: ignore[method-assign]
    service.require_harness = lambda: None  # type: ignore[method-assign]
    service.require_machine_identity = lambda: None  # type: ignore[assignment]
    service.refresh_sources = lambda: None  # type: ignore[method-assign]

    async def _register() -> bool:
        return False

    service._register = _register  # type: ignore[method-assign]

    stop = asyncio.Event()
    running = asyncio.ensure_future(service.run_until(stop))
    await asyncio.sleep(0.05)
    service.gave_up.set()
    await asyncio.wait_for(running, timeout=10.0)

    assert drained == []


# -- a supervised restart is not a replacement --------------------------------


class _Custody:
    """Folder custody whose observable is what it still HOLDS: a lease kept
    across a restart is a folder still held here; a hand-back is one that is
    not. ``stuck`` names a chat whose hand-back does not return until
    ``unstick`` is set, the drive that never answers one folder."""

    def __init__(self, *, stuck: str | None = None) -> None:
        import threading

        self.enabled = True
        self._held: dict[str, Any] = {}
        self.handed_back: list[str] = []
        self.stuck = stuck
        self.unstick = threading.Event()
        self.stuck_entered = threading.Event()

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        from types import SimpleNamespace

        held = SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)
        self._held[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def stop_live(self, chat_id: str, deadline: float = 5.0) -> None:
        return None

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        return chat_id in self._held

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        if chat_id == self.stuck:
            self.stuck_entered.set()
            self.unstick.wait(timeout=20.0)
        self._held.pop(chat_id, None)
        self.handed_back.append(chat_id)
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


@pytest.mark.parametrize(
    ("env", "final_stop", "expected"),
    [
        pytest.param({}, False, (False, None), id="unsupervised"),
        pytest.param({"ALKERA_DAEMON_SUPERVISED": "1"}, False, (True, None), id="supervised"),
        pytest.param({"ALKERA_DAEMON_SUPERVISED": "0"}, False, (False, None), id="said-no"),
        pytest.param({"ALKERA_DAEMON_SUPERVISED": "yes"}, False, (False, None), id="not-one"),
    ],
)
def test_supervision_is_read_from_the_environment(
    env: dict[str, str], final_stop: bool, expected: tuple[bool, Path | None]
) -> None:
    assert supervision_from_env(env) == expected


@pytest.mark.parametrize("provider", ["ec2", "runpod"])
def test_a_provisioned_node_unit_runs_the_daemon_supervised(provider: str) -> None:
    """The environment the node's systemd unit gives the daemon is read as a
    supervised daemon with its final-stop file: a ``systemctl restart`` of a
    node is a restart in place, not a departure that moves its chats to
    another box while this one still holds their folders."""
    script = render_bootstrap(
        BootstrapSpec(
            provider=provider,
            allocation_id="12345678-1234-5678-1234-567812345678",
            machine_name="pool-1",
            type_code="m6i.large",
            tenancy="pool",
            api_url="https://api.example.test",
            release_base_url="https://releases.example.test/",
            credential_secret="alkera/test/node/x" if provider == "ec2" else "",
            region="us-east-1",
        ),
        PLAN,
    )
    service = script[script.index("[Service]") : script.index("[Install]")]
    env = dict(
        line.removeprefix("Environment=").split("=", 1)
        for line in service.splitlines()
        if line.startswith("Environment=")
    )
    assert supervision_from_env(env) == (True, Path(FINAL_STOP_FILE))


def test_the_final_stop_file_is_read_from_the_environment(tmp_path: Path) -> None:
    marker = tmp_path / "final"
    env = {"ALKERA_DAEMON_SUPERVISED": "1", "ALKERA_DAEMON_FINAL_STOP_FILE": str(marker)}
    assert supervision_from_env(env) == (True, marker)


@pytest.mark.parametrize(
    ("supervised", "final_stop", "restarts"),
    [
        pytest.param(True, False, True, id="supervised-restart"),
        pytest.param(True, True, False, id="supervisor-stopping-for-good"),
        pytest.param(False, False, False, id="unsupervised-stop"),
    ],
)
async def test_what_a_stop_does_with_the_folders_this_box_holds(
    tmp_path: Path, supervised: bool, final_stop: bool, restarts: bool
) -> None:
    """A supervised restart keeps every lease and hands nothing back, so the
    process that starts next takes the same chats straight back; every other
    stop hands each chat on, which is what lets another box take it."""
    marker = tmp_path / "final-stop"
    if final_stop:
        marker.write_text("")
    custody = _Custody()
    service, built = build_service(
        tmp_path,
        clock=Clock(),
        folders=custody,
        supervised=supervised,
        final_stop_file=marker,
    )
    service._machine_id = "machine:x"
    for chat_id in ("chat-a", "chat-b"):
        await service._ensure_mirror(chat_id, _chat(chat_id))

    await service.drain()
    await service.stop()

    assert service.restarting is restarts
    assert built["chat-a"].stopped and built["chat-b"].stopped
    if restarts:
        assert custody.handed_back == []
        assert custody.held("chat-a") is not None and custody.held("chat-b") is not None
    else:
        assert sorted(custody.handed_back) == ["chat-a", "chat-b"]
        assert custody.held("chat-a") is None and custody.held("chat-b") is None


async def test_a_supervised_restart_waits_at_most_its_short_ceiling_for_a_running_turn(
    tmp_path: Path,
) -> None:
    """The restart costs the gap it takes, never the turn's length: a turn
    still running at the ceiling is left for the next process to restart."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, supervised=True)
    await service._ensure_mirror("chat-busy", _chat("chat-busy"))
    built["chat-busy"].turn_running = True

    started = clock.now
    await asyncio.wait_for(service.drain(), timeout=10.0)

    assert clock.now - started == pytest.approx(RESTART_DRAIN_CEILING_SECONDS)
    assert service.mirrors == {"chat-busy": built["chat-busy"]}


async def test_the_heartbeat_says_restarting_under_the_supervisor(tmp_path: Path) -> None:
    said: list[dict[str, Any]] = []
    beaten = asyncio.Event()

    async def _beat(machine_id: str, **kwargs: Any) -> dict[str, Any]:
        said.append(kwargs)
        beaten.set()
        return {}

    service, _built = build_service(tmp_path, clock=Clock(), supervised=True)
    service._machine_id = "machine:x"
    service._rest.heartbeat_machine = _beat  # type: ignore[method-assign]
    service.begin_drain()
    task = asyncio.ensure_future(service._machine_loop())
    await asyncio.wait_for(beaten.wait(), timeout=10.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert said[-1]["restarting"] is True
    assert said[-1]["draining"] is False


async def test_one_folder_the_drive_will_not_take_holds_no_other_chats_hand_back(
    tmp_path: Path,
) -> None:
    """The drain hands every idle chat back at once: a hand-back stuck on one
    folder must not keep the next chat on this box."""
    custody = _Custody(stuck="chat-stuck")
    service, _built = build_service(tmp_path, clock=Clock(), folders=custody)
    service._machine_id = "machine:x"
    for chat_id in ("chat-stuck", "chat-free"):
        await service._ensure_mirror(chat_id, _chat(chat_id))

    draining = asyncio.ensure_future(service.drain())
    try:
        await asyncio.wait_for(asyncio.to_thread(custody.stuck_entered.wait, 10.0), 15.0)
        for _ in range(200):
            if "chat-free" in custody.handed_back:
                break
            await asyncio.sleep(0.02)
        assert custody.handed_back == ["chat-free"]
    finally:
        custody.unstick.set()
        await asyncio.wait_for(draining, timeout=20.0)
    assert sorted(custody.handed_back) == ["chat-free", "chat-stuck"]


# -- an org worker's stop, as its supervisor says it ------------------------------


@pytest.mark.parametrize(
    ("frames", "keeps"),
    [
        pytest.param([Stop(final=True)], False, id="final-stop-hands-every-chat-back"),
        pytest.param([Stop(final=False)], True, id="restart-keeps-every-lease"),
        pytest.param([], True, id="supervisor-gone-is-a-restart"),
    ],
)
async def test_an_org_worker_drains_as_its_supervisor_says(
    tmp_path: Path, frames: list[Stop], keeps: bool
) -> None:
    """An org worker is never supervised itself (its settings say so), so the
    kind of its stop is the supervisor's: a final stop hands each chat on, a
    restart keeps them for the worker that starts next, and a supervisor that
    hung up without a word (it was restarted) is a restart too."""
    custody = _Custody()
    service, _built = build_service(tmp_path, clock=Clock(), folders=custody)
    service._machine_id = "machine:x"
    for chat_id in ("chat-a", "chat-b"):
        await service._ensure_mirror(chat_id, _chat(chat_id))
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(encode(frame))
    reader.feed_eof()
    stop = asyncio.Event()

    await serve_control(
        service, reader, stop=stop, on_credential=lambda _frame: None, report=lambda: None
    )
    await service.drain()
    await service.stop()

    assert stop.is_set()
    assert service.restarting is keeps
    if keeps:
        assert custody.handed_back == []
    else:
        assert sorted(custody.handed_back) == ["chat-a", "chat-b"]


# -- a restart in place and the background jobs it holds ---------------------------


async def test_a_restart_waits_for_a_background_job_past_its_short_ceiling(
    tmp_path: Path,
) -> None:
    """A job cannot be restarted by the next process the way a turn can, so a
    restart in place waits for it like a final drain does, keeps the chat (its
    lease held, nothing handed back) and lets the job's reply be delivered:
    the job ends, the model's turn on its result runs, and only then does the
    process go."""
    clock = Clock()
    custody = _Custody()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)
        if clock.now - started >= 200 and mirror.job_running:
            mirror.job_running = False  # the job ended ...
            mirror.turn_running = True  # ... and the model answers its result
        elif clock.now - started >= 210:
            mirror.turn_running = False

    service, built = build_service(
        tmp_path, clock=clock, sleep=_sleep, supervised=True, folders=custody
    )
    service._machine_id = "machine:x"
    service._settings = replace(service._settings, drain_ceiling_seconds=600.0)
    await service._ensure_mirror("chat-job", _chat("chat-job"))
    mirror = built["chat-job"]
    mirror.job_running = True
    started = clock.now

    await asyncio.wait_for(service.drain(), timeout=10.0)

    assert service.restarting is True
    assert 210 <= clock.now - started < 600
    assert not mirror.job_running and not mirror.turn_running
    assert custody.handed_back == []
    assert service.mirrors == {"chat-job": mirror}


async def test_a_restart_stops_waiting_for_a_job_at_the_drain_ceiling(tmp_path: Path) -> None:
    """Past the ceiling the restart goes: the next process tells the chat the
    job was interrupted (``harness/interrupted_jobs.py``)."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    service, built = build_service(tmp_path, clock=clock, sleep=_sleep, supervised=True)
    service._settings = replace(service._settings, drain_ceiling_seconds=600.0)
    await service._ensure_mirror("chat-job", _chat("chat-job"))
    built["chat-job"].job_running = True
    started = clock.now

    await asyncio.wait_for(service.drain(), timeout=10.0)

    assert clock.now - started == pytest.approx(600.0)
    assert service.mirrors == {"chat-job": built["chat-job"]}
