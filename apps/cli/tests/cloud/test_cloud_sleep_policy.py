"""A chat sleeps only when it must.

The box keeps a chat awake until it needs the room: a new chat with every slot
taken, or the chats' memory past its line, sleeps the least recently used idle
chat first. Under that sits a backstop for cost hygiene, a day, which only a
chat with nothing running and nobody using it ever reaches. Anything running
keeps a chat awake whatever the window says: a turn, a background job, an ask
a person may still answer, and a process a command left behind in the chat's
sandbox, which no job list knows about.

The windows are driven with freezegun across their real thresholds (the box
reads the monotonic clock, which freezegun moves). A reader's presence arrives
as the gateway's presence frame on the box's socket. The sandbox test runs a
real process and reads it through the real probe from a cgroup tree that names
it.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import FakeMirror, build_service
from alkera_cli.cloud.service import (
    ENV_MIRROR_IDLE_MINUTES,
    CloudMirrorService,
    mirror_limits_from_env,
)
from alkera_cli.cloud.sleep_policy import (
    DEFAULT_DISK_PRESSURE_PERCENT,
    DEFAULT_MEMORY_PRESSURE_PERCENT,
    DEFAULT_MIRROR_IDLE_MINUTES,
    ENV_MEMORY_PRESSURE_PERCENT,
    SleepSettings,
    chat_of_channel,
    memory_pressure_percent_from_env,
)
from alkera_cli.files.live_sync import LiveEntry
from alkera_cli.harness.sandbox_processes import (
    CgroupLister,
    SandboxProbes,
    SandboxProcess,
    SandboxProcessProbe,
)
from alkera_core.compute.liveness import ENV_CHAT_IDLE_MINUTES
from alkera_core.schemas.realtime import PresenceFrame
from freezegun import freeze_time

GIB = 1024 * 1024 * 1024
#: A weekday evening, so a day's window crosses midnight on the way.
EVENING = "2026-10-05 17:30:00"
LEFT_RUNNING = (SandboxProcess(pid=15, ppid=1, command="sleep", state="S"),)


def _monotonic() -> float:
    # Looked up on every call, so freezegun's patch is what answers.
    return time.monotonic()


class _Sandboxes:
    """What runs in each chat's sandbox besides its agent server, by chat id:
    what the box's probe answers. A chat it does not name has nothing there."""

    def __init__(self) -> None:
        self.work: dict[str, tuple[SandboxProcess, ...] | None] = {}

    def __call__(self, chat_id: str) -> tuple[SandboxProcess, ...] | None:
        return self.work.get(chat_id, ())


def _service(tmp_path: Path, **kwargs: Any) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    return build_service(tmp_path, clock=_monotonic, **kwargs)  # type: ignore[arg-type]


async def _serve(service: CloudMirrorService, *chat_ids: str) -> None:
    for chat_id in chat_ids:
        await service._ensure_mirror(chat_id, {"id": chat_id})


def _reader_present(
    service: CloudMirrorService, chat_id: str, *, event: str = "heartbeat", peer: str = "reader"
) -> None:
    """The gateway's presence frame for a reader on the chat's document, as
    the box's socket hands it to the service (``start`` wires the two)."""
    service.hear_presence(_frame(chat_id, event=event, peer=peer))


def _frame(chat_id: str, *, event: str = "heartbeat", peer: str = "reader") -> PresenceFrame:
    return PresenceFrame(
        channel=f"doc:chat:{chat_id}",
        event=event,  # type: ignore[arg-type]
        peers=[{"peer_id": peer, "user_id": "u-1", "last_seen_at": datetime.now(UTC)}],
    )


def _awake(service: CloudMirrorService) -> set[str]:
    return set(service.mirrors)


# -- the backstop --------------------------------------------------------------


async def test_an_idle_chat_stays_awake_for_a_day_and_sleeps_once_it_has_passed(
    tmp_path: Path,
) -> None:
    """The deployment default, untouched by the test: an hour (the old window)
    is nothing, the chat is still warm the next morning, and only a full day
    with nothing running and nobody using it puts it to sleep."""
    assert DEFAULT_MIRROR_IDLE_MINUTES == 24 * 60
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path)
        await _serve(service, "chat-a")
        await service.sweep_idle_mirrors()

        frozen.tick(timedelta(minutes=61))
        assert await service.sweep_idle_mirrors() == []
        frozen.move_to("2026-10-06 09:00:00")  # the next morning
        assert await service.sweep_idle_mirrors() == []
        frozen.move_to("2026-10-06 17:29:00")
        assert await service.sweep_idle_mirrors() == []
        assert not built["chat-a"].stopped

        frozen.move_to("2026-10-06 17:31:00")
        assert await service.sweep_idle_mirrors() == ["chat-a"]
        assert built["chat-a"].stopped


async def test_a_reader_with_the_chat_open_keeps_it_from_the_backstop(tmp_path: Path) -> None:
    """A reader present on the chat's document is using it, though it runs
    nothing: the day starts again from them. A reader leaving is not use."""
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _service(tmp_path)
        await _serve(service, "chat-a")
        await service.sweep_idle_mirrors()

        frozen.tick(timedelta(hours=23))
        _reader_present(service, "chat-a")
        frozen.tick(timedelta(hours=1))
        _reader_present(service, "chat-a", event="leave")
        frozen.tick(timedelta(hours=1))  # a day past the output, two hours past the reader
        assert await service.sweep_idle_mirrors() == []

        frozen.tick(timedelta(hours=22, minutes=1))  # a day past the reader
        assert await service.sweep_idle_mirrors() == ["chat-a"]


async def test_the_boxs_own_presence_is_not_a_reader(tmp_path: Path) -> None:
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _service(tmp_path)
        service.socket._peer_id = "peer-box"
        await _serve(service, "chat-a")
        await service.sweep_idle_mirrors()

        frozen.tick(timedelta(hours=23))
        _reader_present(service, "chat-a", peer="peer-box")
        frozen.tick(timedelta(hours=1, minutes=1))
        assert await service.sweep_idle_mirrors() == ["chat-a"]


async def test_a_chat_with_anything_running_never_reaches_the_backstop(tmp_path: Path) -> None:
    """A turn, a background job, a process in the sandbox: each holds the chat
    however long the window is, and once it ends the window starts from there."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, sandbox_work=sandboxes)
        await _serve(service, "turn", "job", "process")
        built["turn"].turn_running = True
        built["job"].busy = True
        sandboxes.work["process"] = LEFT_RUNNING
        await service.sweep_idle_mirrors()

        frozen.tick(timedelta(days=3))
        assert await service.sweep_idle_mirrors() == []
        assert _awake(service) == {"turn", "job", "process"}

        built["turn"].end_turn()
        built["job"].busy = False
        sandboxes.work["process"] = ()
        assert await service.sweep_idle_mirrors() == []  # the window starts now, not days ago
        frozen.tick(timedelta(hours=24, minutes=1))
        assert sorted(await service.sweep_idle_mirrors()) == ["job", "process", "turn"]


# -- a process left behind in the sandbox, through the real probe --------------


def _sleeper() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(["sleep", "7200"])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


@pytest.fixture
def left_behind() -> Iterator[subprocess.Popen[bytes]]:
    """The process the chat's command left running."""
    yield from _sleeper()


@pytest.fixture
def agent_server() -> Iterator[subprocess.Popen[bytes]]:
    """Stands in for the chat's agent server, in the same cgroup."""
    yield from _sleeper()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes")
async def test_a_process_left_running_in_the_sandbox_keeps_the_chat_awake_past_the_window(
    tmp_path: Path,
    agent_server: subprocess.Popen[bytes],
    left_behind: subprocess.Popen[bytes],
) -> None:
    """``nohup sleep 7200 &`` returns at once and no job list knows about it.
    The box reads the chat's sandbox itself before it sleeps the chat, finds
    the process and leaves the chat awake; once the process is gone the chat
    sleeps a window after the last time it was seen."""
    cgroup = tmp_path / "cgroup" / "alkera-chat-a.slice"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text(
        f"{agent_server.pid}\n{left_behind.pid}\n", encoding="ascii"
    )
    probe = SandboxProcessProbe(CgroupLister(cgroup, agent_server.pid))

    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, sandbox_work=lambda _chat: probe.work())
        await _serve(service, "chat-a")
        await service.sweep_idle_mirrors()

        frozen.tick(timedelta(hours=25))
        assert await service.sweep_idle_mirrors() == []
        assert not built["chat-a"].stopped

        left_behind.kill()
        left_behind.wait()
        assert await service.sweep_idle_mirrors() == []  # seen an instant ago
        frozen.tick(timedelta(hours=24, minutes=1))
        assert await service.sweep_idle_mirrors() == ["chat-a"]


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(lambda _chat: None, id="cannot-be-read"),
        pytest.param(lambda _chat: 1 / 0, id="raises"),
    ],
)
async def test_a_sandbox_that_cannot_be_read_does_not_pin_the_chat(
    tmp_path: Path, probe: Any
) -> None:
    """A probe that cannot tell (an unsandboxed chat, a container that did not
    answer, a probe that fails) must not keep every chat on the box awake."""
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _service(tmp_path, sandbox_work=probe)
        await _serve(service, "chat-a")
        await service.sweep_idle_mirrors()
        frozen.tick(timedelta(hours=24, minutes=1))
        assert await service.sweep_idle_mirrors() == ["chat-a"]


# -- the box needs the room: least recently used first --------------------------


async def test_a_full_box_sleeps_the_least_recently_used_idle_chat_first(tmp_path: Path) -> None:
    """Three slots, three awake chats, each last used differently: by its last
    output, by a reader present on it, or not at all since it opened. Each new
    chat takes the slot of the one used longest ago; a reader with the chat open
    counts as use, though the chat runs nothing."""
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, max_mirrors=3)
        await _serve(service, "opened-only", "answered", "read")
        frozen.tick(timedelta(hours=1))
        built["answered"].published_count += 1
        await service.sweep_idle_mirrors()
        frozen.tick(timedelta(hours=1))
        _reader_present(service, "read")
        frozen.tick(timedelta(minutes=5))

        await _serve(service, "new-1")
        assert built["opened-only"].stopped
        assert _awake(service) == {"answered", "read", "new-1"}

        await _serve(service, "new-2")
        assert built["answered"].stopped
        assert _awake(service) == {"read", "new-1", "new-2"}


async def test_a_full_box_takes_a_plain_idle_chat_before_one_held_by_its_sandbox(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The least recently used chat has a process left running: the box takes
    the slot of the next one, which runs nothing. Once only held chats are
    left, a held chat's slot is taken rather than the new chat left waiting,
    and the log says it was slept with a process running."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, max_mirrors=2, sandbox_work=sandboxes)
        await _serve(service, "oldest")
        frozen.tick(timedelta(minutes=10))
        await _serve(service, "newer")
        sandboxes.work["oldest"] = LEFT_RUNNING
        frozen.tick(timedelta(minutes=10))

        await _serve(service, "new-1")
        assert not built["oldest"].stopped and built["newer"].stopped
        assert _awake(service) == {"oldest", "new-1"}

        built["new-1"].turn_running = True
        with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.sleep_policy"):
            await _serve(service, "new-2")
        assert _awake(service) == {"new-1", "new-2"}, "a running turn is never the one taken"
        assert "oldest is put to sleep although a process still runs" in caplog.text
        assert "needs a slot" in caplog.text


async def test_a_box_whose_every_slot_is_held_by_a_sandbox_still_makes_room(
    tmp_path: Path,
) -> None:
    """Process-only holds can never fill a shared box: with every slot held
    that way, a new chat takes the slot of the one used longest ago."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, max_mirrors=3, sandbox_work=sandboxes)
        for chat_id in ("a", "b", "c"):
            await _serve(service, chat_id)
            sandboxes.work[chat_id] = LEFT_RUNNING
            frozen.tick(timedelta(minutes=5))
        _reader_present(service, "a")  # the oldest is open in a reader's browser

        await _serve(service, "new")
        assert built["b"].stopped
        assert _awake(service) == {"a", "c", "new"}


async def test_a_sandbox_read_that_fails_once_is_retried_before_an_eviction(
    tmp_path: Path,
) -> None:
    """The least recently used chat's sandbox read fails once and then shows a
    process: read once only, it would have counted as "nothing runs" and been
    slept ahead of a chat that runs nothing."""
    reads: dict[str, int] = {}

    def flaky(chat_id: str) -> tuple[SandboxProcess, ...] | None:
        reads[chat_id] = reads.get(chat_id, 0) + 1
        if chat_id != "held":
            return ()
        return None if reads[chat_id] == 1 else LEFT_RUNNING

    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, max_mirrors=2, sandbox_work=flaky)
        await _serve(service, "held")
        frozen.tick(timedelta(minutes=10))
        await _serve(service, "plain")
        frozen.tick(timedelta(minutes=10))

        await _serve(service, "new")
        assert built["plain"].stopped and not built["held"].stopped


# -- the box needs the room: memory pressure -----------------------------------


class _Memory:
    """The chats' working set: a gibibyte for each awake chat, against a limit."""

    def __init__(self, limit_gib: int) -> None:
        self.limit = limit_gib * GIB
        self.service: CloudMirrorService | None = None

    def __call__(self) -> tuple[int, int] | None:
        assert self.service is not None
        return (len(self.service.mirrors) * GIB, self.limit)


def _memory_service(
    tmp_path: Path, limit_gib: int, **kwargs: Any
) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    memory = _Memory(limit_gib)
    service, built = _service(tmp_path, memory=memory, **kwargs)
    memory.service = service
    return service, built


async def test_memory_past_its_line_sleeps_idle_chats_least_recently_used_first(
    tmp_path: Path,
) -> None:
    """Five awake chats at a gibibyte each on a five-gibibyte box is 100%, past
    the 85% line: the box sleeps the least recently used idle chat, which puts
    it at 80%, under the line, and stops there. A reader with the oldest chat
    open makes the next one the least recently used."""
    assert DEFAULT_MEMORY_PRESSURE_PERCENT == 85
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _memory_service(tmp_path, limit_gib=5)
        for chat_id in ("a", "b", "c", "d", "e"):
            await _serve(service, chat_id)
            frozen.tick(timedelta(minutes=1))
        _reader_present(service, "a")

        assert await service.sweep_idle_mirrors() == ["b"]
        assert _awake(service) == {"a", "c", "d", "e"}


async def test_memory_far_past_its_line_sleeps_one_chat_a_sweep(tmp_path: Path) -> None:
    """A cgroup reading can lag the sleep that freed the memory, so the box
    sleeps one chat per sweep and reads again on the next one."""
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _memory_service(tmp_path, limit_gib=4)  # five of four is 125%
        for chat_id in ("a", "b", "c", "d", "e"):
            await _serve(service, chat_id)
            frozen.tick(timedelta(minutes=1))
        assert await service.sweep_idle_mirrors() == ["a"]
        assert await service.sweep_idle_mirrors() == ["b"]  # three of four is 75%
        assert await service.sweep_idle_mirrors() == []


async def test_a_memory_reading_that_lags_the_sleep_takes_one_chat_not_all(
    tmp_path: Path,
) -> None:
    with freeze_time(EVENING, real_asyncio=True):
        service, _ = _service(tmp_path, memory=lambda: (9 * GIB, 10 * GIB))  # never drops
        await _serve(service, "a", "b", "c")
        assert len(await service.sweep_idle_mirrors()) == 1
        assert len(service.mirrors) == 2


async def test_memory_under_its_line_sleeps_nothing(tmp_path: Path) -> None:
    with freeze_time(EVENING, real_asyncio=True):
        service, _ = _memory_service(tmp_path, limit_gib=6)  # five of six is 83%
        await _serve(service, "a", "b", "c", "d", "e")
        assert await service.sweep_idle_mirrors() == []
        assert len(service.mirrors) == 5


async def test_memory_pressure_never_sleeps_a_turn_a_job_or_an_ask(tmp_path: Path) -> None:
    """Every chat busy in its own way: the box sleeps none of them, however far
    past the line its memory is, and leaves the rest to the kernel's limit."""
    with freeze_time(EVENING, real_asyncio=True):
        service, built = _memory_service(tmp_path, limit_gib=2)
        await _serve(service, "turn", "job", "ask")
        built["turn"].turn_running = True
        built["job"].busy = True
        built["ask"].parked_only = True  # an ask a person may still answer, within its window
        assert await service.sweep_idle_mirrors() == []
        assert _awake(service) == {"turn", "job", "ask"}


async def test_memory_pressure_relieves_a_box_held_only_by_sandbox_processes(
    tmp_path: Path,
) -> None:
    """Process-only holds cannot leave a box to the OOM killer: past the line,
    a chat held only by its sandbox is slept once nothing plainer is left, the
    least recently used of them first; a running turn still never is."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _memory_service(tmp_path, limit_gib=2, sandbox_work=sandboxes)
        for chat_id in ("held-old", "held-new", "turn"):
            await _serve(service, chat_id)
            frozen.tick(timedelta(minutes=5))
        sandboxes.work.update({"held-old": LEFT_RUNNING, "held-new": LEFT_RUNNING})
        built["turn"].turn_running = True

        assert await service.sweep_idle_mirrors() == ["held-old"]
        assert await service.sweep_idle_mirrors() == ["held-new"]
        assert _awake(service) == {"turn"}


async def test_memory_that_cannot_be_read_sleeps_nothing(tmp_path: Path) -> None:
    with freeze_time(EVENING, real_asyncio=True):
        service, _ = _service(tmp_path, memory=lambda: None)
        await _serve(service, "a", "b")
        assert await service.sweep_idle_mirrors() == []


# -- a drain does not cut a process the chat left running -----------------------


async def test_a_drain_holds_a_chat_whose_sandbox_still_runs_work(tmp_path: Path) -> None:
    """A deploy hands back every quiet chat at once; a chat with a process left
    in its sandbox is waited for, but only for the drain's process budget."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True):
        service, built = _service(tmp_path, sandbox_work=sandboxes)
        await _serve(service, "quiet", "process")
        sandboxes.work["process"] = LEFT_RUNNING

        await service.drain(ceiling=0)

        assert built["quiet"].stopped
        assert not built["process"].stopped


async def test_a_drain_waits_minutes_not_hours_for_a_process_and_reads_it_rarely(
    tmp_path: Path,
) -> None:
    """A forgotten background server must not hold a deploy for the six-hour
    ceiling: the chat is handed back once the process budget (five minutes) is
    spent, and its sandbox is read on a doubling interval, not every poll."""
    reads = 0

    def always_running(_chat: str) -> tuple[SandboxProcess, ...]:
        nonlocal reads
        reads += 1
        return LEFT_RUNNING

    with freeze_time(EVENING, real_asyncio=True) as frozen:

        async def _sleep(seconds: float) -> None:
            frozen.tick(timedelta(seconds=seconds))
            await asyncio.sleep(0)

        service, built = _service(tmp_path, sandbox_work=always_running, sleep=_sleep)
        await _serve(service, "process")
        started = time.monotonic()

        await asyncio.wait_for(service.drain(), timeout=10.0)

        waited = time.monotonic() - started
        assert built["process"].stopped
        assert 300 <= waited < 310, f"handed back after {waited:.0f} s"
        # Every two-second poll would be 150 reads; doubling to a minute is ten.
        assert reads <= 12


async def test_a_chat_used_while_its_sandbox_was_read_is_not_slept(tmp_path: Path) -> None:
    """Reading a sandbox takes time, and a message can land meanwhile: the
    backstop looks again after the read before it sleeps the chat."""
    built: dict[str, FakeMirror] = {}

    def a_turn_starts(chat_id: str) -> tuple[SandboxProcess, ...]:
        built[chat_id].turn_running = True
        return ()

    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, made = _service(tmp_path, sandbox_work=a_turn_starts)
        await _serve(service, "chat-a")
        built.update(made)
        await service.sweep_idle_mirrors()
        frozen.tick(timedelta(hours=24, minutes=1))

        assert await service.sweep_idle_mirrors() == []
        assert not built["chat-a"].stopped


# -- the settings ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "minutes"),
    [
        pytest.param({}, 1440.0, id="unset-is-a-day"),
        pytest.param({ENV_CHAT_IDLE_MINUTES: "720"}, 720.0, id="the-platform-serves-it"),
        pytest.param({ENV_MIRROR_IDLE_MINUTES: "90"}, 90.0, id="the-older-name-still-works"),
        pytest.param(
            {ENV_CHAT_IDLE_MINUTES: "720", ENV_MIRROR_IDLE_MINUTES: "90"},
            720.0,
            id="the-platform-name-wins",
        ),
        pytest.param(
            {ENV_CHAT_IDLE_MINUTES: "nope", ENV_MIRROR_IDLE_MINUTES: "90"},
            90.0,
            id="an-unusable-platform-value-falls-to-the-older-name",
        ),
        pytest.param({ENV_CHAT_IDLE_MINUTES: "0"}, 1440.0, id="zero-is-not-never"),
    ],
)
def test_the_idle_window_is_read_from_the_platform_then_the_older_name(
    env: dict[str, str], minutes: float
) -> None:
    assert mirror_limits_from_env(env)[0] == minutes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 85.0, id="unset"),
        pytest.param("70", 70.0, id="an-operator-line"),
        pytest.param("100", 100.0, id="the-kernel-alone"),
        pytest.param("0", 85.0, id="zero-would-sleep-everything"),
        pytest.param("101", 85.0, id="past-the-limit"),
        pytest.param("-5", 85.0, id="negative"),
        pytest.param("nan", 85.0, id="not-finite"),
        pytest.param("lots", 85.0, id="not-a-number"),
    ],
)
def test_the_memory_pressure_line_is_a_setting(raw: str | None, expected: float) -> None:
    env = {} if raw is None else {ENV_MEMORY_PRESSURE_PERCENT: raw}
    assert memory_pressure_percent_from_env(env) == expected


async def test_the_socket_hands_every_presence_frame_to_its_listeners(tmp_path: Path) -> None:
    """The gateway sends a channel's presence to every subscribed socket; the
    box holds no presence of its own, so this is how it hears a reader."""
    service, _ = _service(tmp_path)
    heard: list[PresenceFrame] = []
    service.socket.on_presence(heard.append)
    await service.socket._on_message(_frame("chat-a").model_dump_json())
    assert [(f.channel, f.event) for f in heard] == [("doc:chat:chat-a", "heartbeat")]


@pytest.mark.parametrize(
    ("channel", "chat"),
    [
        pytest.param("doc:chat:c-1", "c-1", id="a-chat-document"),
        pytest.param("doc:report:r-1", None, id="another-kind-of-document"),
        pytest.param("machine:m-1", None, id="not-a-document"),
        pytest.param("doc:chat:", None, id="no-id"),
    ],
)
def test_presence_is_read_only_off_a_chat_document(channel: str, chat: str | None) -> None:
    assert chat_of_channel(channel) == chat


# -- an edit applied to the chat's files is use ---------------------------------


class _Folders:
    """Custody whose live sync applies what the drive holds for a chat: an
    entry per file a person saved (from an editor's working copy, or dropped
    in the drive), in the state the sync reached for it."""

    def __init__(self) -> None:
        self.enabled = True
        self.inbound: dict[str, list[LiveEntry]] = {}
        self._held: dict[str, Any] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        self._held[chat_id] = SimpleNamespace(
            record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None
        )
        return self._held[chat_id]

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        held = self._held[chat_id]
        held.live = SimpleNamespace(pull_inbound=lambda: self.inbound.pop(chat_id, []))
        return held.live

    def stop_live(self, chat_id: str, deadline: float = 5.0) -> None:
        return None

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        return chat_id in self._held

    def push(self, chat_id: str) -> Any:
        return None

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._held.pop(chat_id, None)
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


@pytest.mark.parametrize(
    ("state", "kept"),
    [
        pytest.param("applied", "edited", id="an-applied-edit-is-use"),
        pytest.param("conflict", "plain", id="an-edit-that-did-not-land-is-not"),
    ],
)
async def test_an_edit_applied_to_a_chats_files_counts_as_use(
    tmp_path: Path, state: str, kept: str
) -> None:
    """A person working on a chat's files from an editor puts nothing on the
    chat's document, but each save the box applies is use: the chat edited a
    minute ago is not the one a full box sleeps first."""
    folders = _Folders()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _service(tmp_path, max_mirrors=2, folders=folders)
        await _serve(service, "edited")
        frozen.tick(timedelta(minutes=10))
        await _serve(service, "plain")
        frozen.tick(timedelta(minutes=10))
        folders.inbound["edited"] = [LiveEntry(node_id="node-notes", state=state)]
        await service._drain_inbound("edited")
        frozen.tick(timedelta(minutes=1))

        await _serve(service, "new")
        assert _awake(service) == {kept, "new"}


class _Lister:
    """A sandbox whose table names one process besides the agent server."""

    agent_pids = frozenset({1})

    def __init__(self) -> None:
        self.running = True

    def list_processes(self) -> tuple[SandboxProcess, ...] | None:
        agent = SandboxProcess(pid=1, ppid=0, command="opencode", state="S")
        return (agent, *LEFT_RUNNING) if self.running else (agent,)


async def test_the_box_reads_the_registry_its_agents_write_and_drops_a_chat_that_left(
    tmp_path: Path,
) -> None:
    """One registry, keyed by the chat's id: the agent server's launch writes
    a probe into it, the box reads it before a sleep, and a chat that left the
    box is dropped from it."""
    probes = SandboxProbes()
    lister = _Lister()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, _ = _service(tmp_path, sandbox_work=probes)
        await _serve(service, "chat-a")
        probes.register("chat-a", SandboxProcessProbe(lister))
        await service.sweep_idle_mirrors()

        frozen.tick(timedelta(hours=25))
        assert await service.sweep_idle_mirrors() == []

        lister.running = False
        frozen.tick(timedelta(hours=24, minutes=1))
        assert await service.sweep_idle_mirrors() == ["chat-a"]
        assert probes.work("chat-a") is None, "the chat left the box; its probe went with it"


async def test_a_job_the_agent_just_started_gets_a_fair_start_before_it_yields(
    tmp_path: Path,
) -> None:
    """A chat whose only hold is a process is evictable under pressure, but not
    in the first minutes after its last turn (the turn that started the job):
    a box under load slept such chats half a minute after the job began. Past
    the residency the box yields it."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, max_mirrors=2, sandbox_work=sandboxes)
        await _serve(service, "job", "turn")
        sandboxes.work["job"] = LEFT_RUNNING
        built["turn"].turn_running = True
        frozen.tick(timedelta(minutes=1))

        await _serve(service, "new")
        assert _awake(service) == {"job", "turn"}, "a one-minute-old job is not taken"

        frozen.tick(timedelta(minutes=5))
        await _serve(service, "new")
        assert _awake(service) == {"turn", "new"}


#: What a box that sets none of its sleep knobs runs with.
BOX_DEFAULTS = SleepSettings(disk_pressure_percent=DEFAULT_DISK_PRESSURE_PERCENT)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, BOX_DEFAULTS, id="unset-is-the-defaults"),
        pytest.param(
            {"ALKERA_CLOUD_PROCESS_HOLD_MIN_SECONDS": "60"},
            replace(BOX_DEFAULTS, process_hold_min_seconds=60.0),
            id="a-shorter-residency",
        ),
        pytest.param(
            {"ALKERA_CLOUD_PROCESS_HOLD_MIN_SECONDS": "-1"},
            BOX_DEFAULTS,
            id="a-negative-residency-is-the-default",
        ),
        pytest.param(
            {"ALKERA_CLOUD_DRAIN_PROCESS_HOLD_SECONDS": "0"},
            replace(BOX_DEFAULTS, drain_process_hold_seconds=0.0),
            id="a-drain-that-waits-for-no-process",
        ),
        pytest.param(
            {"ALKERA_CLOUD_DISK_PRESSURE_PERCENT": "0"},
            replace(BOX_DEFAULTS, disk_pressure_percent=0.0),
            id="the-disk-valve-off",
        ),
        pytest.param(
            {"ALKERA_CLOUD_DISK_PRESSURE_PERCENT": "150"},
            BOX_DEFAULTS,
            id="a-disk-line-past-100-is-the-default",
        ),
        pytest.param(
            {"ALKERA_CLOUD_SLOT_EVICTION": "off"},
            replace(BOX_DEFAULTS, slot_eviction=False),
            id="slot-eviction-off",
        ),
        pytest.param(
            {"ALKERA_CLOUD_SLOT_EVICTION": "yes"},
            BOX_DEFAULTS,
            id="slot-eviction-on-for-anything-else",
        ),
    ],
)
def test_the_box_side_sleep_knobs_are_read_from_its_environment(
    env: dict[str, str], expected: SleepSettings
) -> None:
    assert SleepSettings.from_env(env) == expected


# -- the valves beside memory ----------------------------------------------------


async def test_a_disk_past_its_line_sleeps_idle_chats_one_a_sweep(tmp_path: Path) -> None:
    """The disk the chats' folders live on counts as pressure the way memory
    does: past its line, the least recently used idle chat is slept, one per
    sweep. The real disk under the test is past a 1% line; with the valve off
    (its default for a policy built in code) the same box sleeps nothing."""
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        off, _ = _service(tmp_path / "off")
        await _serve(off, "a", "b")
        assert await off.sweep_idle_mirrors() == []

        on, _ = _service(tmp_path / "on", sleep_settings=SleepSettings(disk_pressure_percent=1.0))
        for chat_id in ("a", "b", "c"):
            await _serve(on, chat_id)
            frozen.tick(timedelta(minutes=1))
        assert await on.sweep_idle_mirrors() == ["a"]
        assert await on.sweep_idle_mirrors() == ["b"]


async def test_a_disk_past_its_line_never_sleeps_a_running_turn(tmp_path: Path) -> None:
    with freeze_time(EVENING, real_asyncio=True):
        service, built = _service(tmp_path, sleep_settings=SleepSettings(disk_pressure_percent=1.0))
        await _serve(service, "busy")
        built["busy"].turn_running = True
        assert await service.sweep_idle_mirrors() == []
        assert _awake(service) == {"busy"}


def test_the_disk_reading_names_which_resource_is_past_its_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from alkera_cli.cloud.sleep_policy import Pressure, SleepPolicy

    disk = [(95 * GIB, 100 * GIB)]
    policy = SleepPolicy(
        idle_minutes=60,
        parked_ask_hours=24,
        clock=time.monotonic,
        memory=lambda: (1 * GIB, 10 * GIB),
        probes=SandboxProbes(),
        settings=SleepSettings(disk_pressure_percent=90.0),
        disk=lambda: disk[0],
    )
    with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.sleep_policy"):
        assert policy.pressure() == Pressure(95 * GIB, 100 * GIB, "disk")
        assert policy.pressure() is not None
        disk[0] = (50 * GIB, 100 * GIB)
        assert policy.pressure() is None
    said = [r.getMessage() for r in caplog.records]
    assert sum("disk is at" in m for m in said) == 1, "said once per episode"
    assert any("back under its line" in m for m in said)


async def test_with_slot_eviction_off_a_new_chat_waits_and_nothing_is_slept(
    tmp_path: Path,
) -> None:
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(
            tmp_path, max_mirrors=2, sleep_settings=SleepSettings(slot_eviction=False)
        )
        await _serve(service, "a", "b")
        frozen.tick(timedelta(hours=2))

        await _serve(service, "new")

        assert _awake(service) == {"a", "b"}
        assert not built["a"].stopped and not built["b"].stopped


async def test_a_forced_sleep_log_names_the_chat_and_its_processes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A sleep that kills a sandbox's processes is logged with the chat's id
    as a field, and the processes it took, so an operator can find it."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, max_mirrors=1, sandbox_work=sandboxes)
        await _serve(service, "held")
        sandboxes.work["held"] = LEFT_RUNNING
        frozen.tick(timedelta(minutes=30))
        with caplog.at_level(logging.INFO, logger="alkera_cli.cloud.sleep_policy"):
            await _serve(service, "new")
        assert built["held"].stopped
    records = [r for r in caplog.records if getattr(r, "chat_id", None) == "held"]
    assert any("still run in its sandbox" in r.getMessage() for r in records)
    assert any("put to sleep although a process" in r.getMessage() for r in records)
    assert any("15 sleep" in str(getattr(r, "processes", "")) for r in records)


# -- a sleep for room never stops work silently ---------------------------------


async def test_a_pressure_sleep_that_stops_sandbox_processes_tells_the_chat(
    tmp_path: Path,
) -> None:
    """Memory past its line and every idle chat held by a process in its
    sandbox: the one slept is told, before it is released, that the box was
    short on memory and which processes the sleep stopped."""
    sandboxes = _Sandboxes()
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(
            tmp_path, memory=lambda: (9 * GIB, 10 * GIB), sandbox_work=sandboxes
        )
        await _serve(service, "held", "busy")
        sandboxes.work["held"] = LEFT_RUNNING
        built["busy"].turn_running = True
        frozen.tick(timedelta(minutes=30))

        assert await service.sweep_idle_mirrors() == ["held"]

    assert built["held"].stopped
    assert built["held"].notes == [
        "This chat was put to sleep because its box was short on memory, "
        "which stopped 1 process still running in it: sleep."
    ]
    assert built["busy"].notes == []


async def test_a_disk_pressure_sleep_names_the_disk(tmp_path: Path) -> None:
    sandboxes = _Sandboxes()
    two = (
        *LEFT_RUNNING,
        SandboxProcess(pid=16, ppid=1, command="/usr/bin/python3 -m http.server", state="S"),
    )
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(
            tmp_path,
            sandbox_work=sandboxes,
            sleep_settings=SleepSettings(disk_pressure_percent=1.0),
        )
        await _serve(service, "held")
        sandboxes.work["held"] = two
        frozen.tick(timedelta(minutes=30))
        assert await service.sweep_idle_mirrors() == ["held"]
    assert built["held"].notes == [
        "This chat was put to sleep because its box was short on disk, "
        "which stopped 2 processes still running in it: sleep, python3."
    ]


async def test_a_pressure_sleep_with_nothing_running_says_nothing(tmp_path: Path) -> None:
    with freeze_time(EVENING, real_asyncio=True) as frozen:
        service, built = _service(tmp_path, memory=lambda: (9 * GIB, 10 * GIB))
        await _serve(service, "quiet", "other")
        frozen.tick(timedelta(minutes=30))
        assert await service.sweep_idle_mirrors() == ["quiet"]
    assert built["quiet"].stopped
    assert built["quiet"].notes == [] and built["other"].notes == []
